"""This module provides custom python preconditioners utilizing
the Slate language.
"""
import ufl

from firedrake.logging import log, WARNING
from firedrake.matrix_free.preconditioners import PCBase
from firedrake.matrix_free.operators import ImplicitMatrixContext
from firedrake.petsc import PETSc
from firedrake.slate.slate import Tensor, AssembledVector
from pyop2.profiling import timed_region, timed_function


__all__ = ['HybridizationPC', 'HybridStaticCondensationPC']


# TODO: Better name?
class HybridStaticCondensationPC(PCBase):
    """
    Expects a fully formulated hybridized formulation
    written in "cell-local" form (no jumps or averages).
    """

    @timed_function("HybridSCInit")
    def initialize(self, pc):
        """
        """
        from firedrake.assemble import (allocate_matrix,
                                        create_assembly_callable)
        from firedrake.bcs import DirichletBC
        from firedrake.formmanipulation import ExtractSubBlock
        from firedrake.function import Function
        from firedrake.functionspace import FunctionSpace
        from firedrake.interpolation import interpolate
        from pyop2.base import MixedDat

        prefix = pc.getOptionsPrefix() + "hybrid_sc_"
        _, P = pc.getOperators()
        self.cxt = P.getPythonContext()
        if not isinstance(self.cxt, ImplicitMatrixContext):
            raise ValueError("Context must be an ImplicitMatrixContext")

        # Retrieve the mixed function space, which is expected to
        # be of the form: W = (DG_k)^n \times DG_k \times DG_trace
        W = self.cxt.a.arguments()[0].function_space()
        if len(W) != 3:
            raise RuntimeError("Expecting three function spaces.")

        # Assert a specific ordering of the spaces
        # TODO: Clean this up
        assert W[2].ufl_element().family() == "HDiv Trace"

        # This operator has the form:
        # | A  B  C |
        # | D  E  F |
        # | G  H  J |
        # NOTE: It is often the case that D = B.T,
        # G = C.T, H = F.T, and J = 0, but we're not making
        # that assumption here.
        splitter = ExtractSubBlock()

        # Extract sub-block:
        # | A B |
        # | D E |
        # which has block row indices (0, 1) and block
        # column indices (0, 1) as well.
        M = Tensor(splitter.split(self.cxt.a, ((0, 1), (0, 1))))

        # Extract sub-block:
        # | C |
        # | F |
        # which has block row indices (0, 1) and block
        # column indices (2,)
        K = Tensor(splitter.split(self.cxt.a, ((0, 1), (2,))))

        # Extract sub-block:
        # | G H |
        # which has block row indices (2,) and block column
        # indices (0, 1)
        L = Tensor(splitter.split(self.cxt.a, ((2,), (0, 1))))

        # And the final block J has block row-column
        # indices (2, 2)
        J = Tensor(splitter.split(self.cxt.a, (2, 2)))

        # Schur complement for traces
        S = J - L * M.inv * K

        # Extract trace space
        T = W[2]

        # Need to duplicate a trace space which is NOT
        # associated with a subspace of a mixed space.
        Tr = FunctionSpace(T.mesh(), T.ufl_element())
        bcs = []
        cxt_bcs = self.cxt.row_bcs
        if cxt_bcs:
            bc_msg = "It is highly recommended to apply BCs weakly."
            log(WARNING, bc_msg)
        for bc in cxt_bcs:
            assert bc.function_space() == T, (
                "BCs should be imposing vanishing conditions on traces"
            )
            if isinstance(bc.function_arg, Function):
                bc_arg = interpolate(bc.function_arg, Tr)
            else:
                # Constants don't need to be interpolated
                bc_arg = bc.function_arg
            bcs.append(DirichletBC(Tr, bc_arg, bc.sub_domain))

        self.S = allocate_matrix(S,
                                 bcs=bcs,
                                 form_compiler_parameters=self.cxt.fc_params)
        self._assemble_S = create_assembly_callable(
            S,
            tensor=self.S,
            bcs=bcs,
            form_compiler_parameters=self.cxt.fc_params)

        self._assemble_S()
        self.S.force_evaluation()
        Smat = self.S.petscmat

        # Set up ksp for the trace problem
        trace_ksp = PETSc.KSP().create(comm=pc.comm)
        trace_ksp.setOptionsPrefix(prefix)
        trace_ksp.setOperators(Smat)
        trace_ksp.setUp()
        trace_ksp.setFromOptions()
        self.trace_ksp = trace_ksp

        # Local tensors needed for reconstruction
        # (extracted by row-column indices
        A = Tensor(splitter.split(self.cxt.a, (0, 0)))
        B = Tensor(splitter.split(self.cxt.a, (0, 1)))
        C = Tensor(splitter.split(self.cxt.a, (0, 2)))
        D = Tensor(splitter.split(self.cxt.a, (1, 0)))
        E = Tensor(splitter.split(self.cxt.a, (1, 1)))
        F = Tensor(splitter.split(self.cxt.a, (1, 2)))
        Se = E - D * A.inv * B
        Sf = F - D * A.inv * C

        # Expression for RHS assembly
        # Set up the functions for trace solution and
        # the residual for the trace system
        self.r_lambda = Function(T)
        self.residual = Function(W)
        self.r_lambda_thunk = Function(T)
        v1, v2, v3 = self.residual.split()

        # Create mixed function for residual computation.
        # This projects the non-trace residual bits into
        # the trace space:
        # -L * M.inv * | v1 v2 |^T
        VV = W[0] * W[1]
        mdat = MixedDat([v1.dat, v2.dat])
        v1v2 = Function(VV, val=mdat.data)
        r_lambda_thunk = -L * M.inv * AssembledVector(v1v2)
        self._assemble_Srhs_thunk = create_assembly_callable(
            r_lambda_thunk,
            tensor=self.r_lambda_thunk,
            form_compiler_parameters=self.cxt.fc_params)

        self.solution = Function(W)
        q_h, u_h, lambda_h = self.solution.split()

        # Assemble u_h using lambda_h
        self._assemble_u = create_assembly_callable(
            Se.inv * (AssembledVector(v2) -
                      D * A.inv * AssembledVector(v1) -
                      Sf * AssembledVector(lambda_h)),
            tensor=u_h,
            form_compiler_parameters=self.cxt.fc_params)

        # Recover q_h using both u_h and lambda_h
        self._assemble_q = create_assembly_callable(
            A.inv * (AssembledVector(v1) -
                     B * AssembledVector(u_h) -
                     C * AssembledVector(lambda_h)),
            tensor=q_h,
            form_compiler_parameters=self.cxt.fc_params)

    @timed_function("HybridSCUpdate")
    def update(self, pc):
        """
        """
        self._assemble_S()
        self.S.force_evaluation()

    def apply(self, pc, x, y):
        """
        """
        with self.residual.dat.vec_wo as v:
            x.copy(v)

        with timed_region("HybridSCRHS"):
            # Now assemble residual for the reduced problem
            self._assemble_Srhs_thunk()

            # Generate the trace RHS residual
            self.r_lambda.assign(self.residual.split()[2] +
                                 self.r_lambda_thunk)

        with timed_region("HybridSCSolve"):
            # Solve the system for the Lagrange multipliers
            with self.r_lambda.dat.vec_ro as b:
                if self.trace_ksp.getInitialGuessNonzero():
                    acc = self.solution.split()[2].dat.vec
                else:
                    acc = self.solution.split()[2].dat.vec_wo
                with acc as x_trace:
                    self.trace_ksp.solve(b, x_trace)

        with timed_region("HybridSCReconstruct"):
            # Recover u_h and q_h
            self._assemble_u()
            self._assemble_q()

        with self.solution.dat.vec_ro as w:
            w.copy(y)

    def applyTranspose(self, pc, x, y):
        """
        """
        raise NotImplementedError("Transpose application is not implemented.")


class HybridizationPC(PCBase):
    """A Slate-based python preconditioner that solves a
    mixed H(div)-conforming problem using hybridization.
    Currently, this preconditioner supports the hybridization
    of the RT and BDM mixed methods of arbitrary degree.

    The forward eliminations and backwards reconstructions
    are performed element-local using the Slate language.
    """

    @timed_function("HybridInit")
    def initialize(self, pc):
        """Set up the problem context. Take the original
        mixed problem and reformulate the problem as a
        hybridized mixed system.

        A KSP is created for the Lagrange multiplier system.
        """
        from firedrake import (FunctionSpace, Function, Constant,
                               TrialFunction, TrialFunctions, TestFunction,
                               DirichletBC, assemble, Projector)
        from firedrake.assemble import (allocate_matrix,
                                        create_assembly_callable)
        from firedrake.formmanipulation import split_form
        from ufl.algorithms.replace import replace

        # Extract the problem context
        prefix = pc.getOptionsPrefix() + "hybridization_"
        _, P = pc.getOperators()
        self.cxt = P.getPythonContext()

        if not isinstance(self.cxt, ImplicitMatrixContext):
            raise ValueError("Context must be an ImplicitMatrixContext")

        test, trial = self.cxt.a.arguments()

        V = test.function_space()
        mesh = V.mesh()

        if len(V) != 2:
            raise ValueError("Expecting two function spaces.")

        if all(Vi.ufl_element().value_shape() for Vi in V):
            raise ValueError("Expecting an H(div) x L2 pair of spaces.")

        # Automagically determine which spaces are vector and scalar
        for i, Vi in enumerate(V):
            if Vi.ufl_element().sobolev_space().name == "HDiv":
                self.vidx = i
            else:
                assert Vi.ufl_element().sobolev_space().name == "L2"
                self.pidx = i

        # Create the space of approximate traces.
        W = V[self.vidx]
        if W.ufl_element().family() == "Brezzi-Douglas-Marini":
            tdegree = W.ufl_element().degree()
        else:
            try:
                # If we have a tensor product element
                h_deg, v_deg = W.ufl_element().degree()
                tdegree = (h_deg - 1, v_deg - 1)
            except TypeError:
                tdegree = W.ufl_element().degree() - 1

        TraceSpace = FunctionSpace(mesh, "HDiv Trace", tdegree)

        # Break the function spaces and define fully discontinuous spaces
        broken_elements = [ufl.BrokenElement(Vi.ufl_element()) for Vi in V]
        V_d = FunctionSpace(mesh, ufl.MixedElement(broken_elements))

        # Set up the functions for the original, hybridized
        # and schur complement systems
        self.broken_solution = Function(V_d)
        self.broken_residual = Function(V_d)
        self.trace_solution = Function(TraceSpace)
        self.unbroken_solution = Function(V)
        self.unbroken_residual = Function(V)

        # Set up the KSP for the hdiv residual projection
        hdiv_mass_ksp = PETSc.KSP().create(comm=pc.comm)
        hdiv_mass_ksp.setOptionsPrefix(prefix + "hdiv_residual_")

        # HDiv mass operator
        p = TrialFunction(V[self.vidx])
        q = TestFunction(V[self.vidx])
        mass = ufl.dot(p, q)*ufl.dx
        M = assemble(mass, form_compiler_parameters=self.cxt.fc_params)
        M.force_evaluation()
        Mmat = M.petscmat

        hdiv_mass_ksp.setOperators(Mmat)
        hdiv_mass_ksp.setUp()
        hdiv_mass_ksp.setFromOptions()
        self.hdiv_mass_ksp = hdiv_mass_ksp

        # Storing the result of A.inv * r, where A is the HDiv
        # mass matrix and r is the HDiv residual
        self._primal_r = Function(V[self.vidx])

        tau = TestFunction(V_d[self.vidx])
        self._assemble_broken_r = create_assembly_callable(
            ufl.dot(self._primal_r, tau)*ufl.dx,
            tensor=self.broken_residual.split()[self.vidx],
            form_compiler_parameters=self.cxt.fc_params)

        # Create the symbolic Schur-reduction:
        # Original mixed operator replaced with "broken"
        # arguments
        arg_map = {test: TestFunction(V_d),
                   trial: TrialFunction(V_d)}
        Atilde = Tensor(replace(self.cxt.a, arg_map))
        gammar = TestFunction(TraceSpace)
        n = ufl.FacetNormal(mesh)
        sigma = TrialFunctions(V_d)[self.vidx]

        # We zero out the contribution of the trace variables on the exterior
        # boundary. Extruded cells will have both horizontal and vertical
        # facets
        if mesh.cell_set._extruded:
            trace_bcs = [DirichletBC(TraceSpace, Constant(0.0), "on_boundary"),
                         DirichletBC(TraceSpace, Constant(0.0), "bottom"),
                         DirichletBC(TraceSpace, Constant(0.0), "top")]
            K = Tensor(gammar('+') * ufl.jump(sigma, n=n) * ufl.dS_h +
                       gammar('+') * ufl.jump(sigma, n=n) * ufl.dS_v)
        else:
            trace_bcs = [DirichletBC(TraceSpace, Constant(0.0), "on_boundary")]
            K = Tensor(gammar('+') * ufl.jump(sigma, n=n) * ufl.dS)

        # If boundary conditions are contained in the ImplicitMatrixContext:
        if self.cxt.row_bcs:
            bc_msg = "Boundary conditions should be applied weakly."
            log(WARNING, bc_msg)

        # Assemble the Schur complement operator and right-hand side
        self.schur_rhs = Function(TraceSpace)
        self._assemble_Srhs = create_assembly_callable(
            K * Atilde.inv * AssembledVector(self.broken_residual),
            tensor=self.schur_rhs,
            form_compiler_parameters=self.cxt.fc_params)

        schur_comp = K * Atilde.inv * K.T
        self.S = allocate_matrix(schur_comp, bcs=trace_bcs,
                                 form_compiler_parameters=self.cxt.fc_params)
        self._assemble_S = create_assembly_callable(schur_comp,
                                                    tensor=self.S,
                                                    bcs=trace_bcs,
                                                    form_compiler_parameters=self.cxt.fc_params)

        self._assemble_S()
        self.S.force_evaluation()
        Smat = self.S.petscmat

        # Nullspace for the multiplier problem
        nullspace = create_schur_nullspace(P, -K * Atilde,
                                           V, V_d, TraceSpace,
                                           pc.comm)
        if nullspace:
            Smat.setNullSpace(nullspace)

        # Set up the KSP for the system of Lagrange multipliers
        trace_ksp = PETSc.KSP().create(comm=pc.comm)
        trace_ksp.setOptionsPrefix(prefix)
        trace_ksp.setOperators(Smat)
        trace_ksp.setUp()
        trace_ksp.setFromOptions()
        self.trace_ksp = trace_ksp

        split_mixed_op = dict(split_form(Atilde.form))
        split_trace_op = dict(split_form(K.form))

        # Generate reconstruction calls
        self._reconstruction_calls(split_mixed_op, split_trace_op)

        # NOTE: The projection stage *might* be replaced by a Fortin
        # operator. We may want to allow the user to specify if they
        # wish to use a Fortin operator over a projection, or vice-versa.
        # In a future add-on, we can add a switch which chooses either
        # the Fortin reconstruction or the usual KSP projection.

        # Set up the projection KSP
        hdiv_projection_ksp = PETSc.KSP().create(comm=pc.comm)
        hdiv_projection_ksp.setOptionsPrefix(prefix + 'hdiv_projection_')

        # Reuse the mass operator from the hdiv_mass_ksp
        hdiv_projection_ksp.setOperators(Mmat)

        # Construct the RHS for the projection stage
        self._projection_rhs = Function(V[self.vidx])
        self._assemble_projection_rhs = create_assembly_callable(
            ufl.dot(self.broken_solution.split()[self.vidx], q)*ufl.dx,
            tensor=self._projection_rhs,
            form_compiler_parameters=self.cxt.fc_params)

        # Finalize ksp setup
        hdiv_projection_ksp.setUp()
        hdiv_projection_ksp.setFromOptions()
        self.hdiv_projection_ksp = hdiv_projection_ksp

        # Switch to using the averaging method over a KSP to get the
        # HDiv conforming solution
        opts = PETSc.Options()
        projection_prefix = prefix + 'hdiv_projection_'
        self._method = opts.getString(projection_prefix + 'method', 'L2')
        if self._method == 'average':
            self.averager = Projector(self.broken_solution.split()[self.vidx],
                                      self.unbroken_solution.split()[self.vidx],
                                      method=self._method)
        else:
            assert self._method == 'L2', (
                "Only projection methods 'L2' and 'average' are supported."
            )

    def _reconstruction_calls(self, split_mixed_op, split_trace_op):
        """This generates the reconstruction calls for the unknowns using the
        Lagrange multipliers.

        :arg split_mixed_op: a ``dict`` of split forms that make up the broken
                             mixed operator from the original problem.
        :arg split_trace_op: a ``dict`` of split forms that make up the trace
                             contribution in the hybridized mixed system.
        """
        from firedrake.assemble import create_assembly_callable

        # We always eliminate the velocity block first
        id0, id1 = (self.vidx, self.pidx)

        # TODO: When PyOP2 is able to write into mixed dats,
        # the reconstruction expressions can simplify into
        # one clean expression.
        A = Tensor(split_mixed_op[(id0, id0)])
        B = Tensor(split_mixed_op[(id0, id1)])
        C = Tensor(split_mixed_op[(id1, id0)])
        D = Tensor(split_mixed_op[(id1, id1)])
        K_0 = Tensor(split_trace_op[(0, id0)])
        K_1 = Tensor(split_trace_op[(0, id1)])

        # Split functions and reconstruct each bit separately
        split_residual = self.broken_residual.split()
        split_sol = self.broken_solution.split()
        g = AssembledVector(split_residual[id0])
        f = AssembledVector(split_residual[id1])
        sigma = split_sol[id0]
        u = split_sol[id1]
        lambdar = AssembledVector(self.trace_solution)

        M = D - C * A.inv * B
        R = K_1.T - C * A.inv * K_0.T
        u_rec = M.inv * (f - C * A.inv * g - R * lambdar)
        self._sub_unknown = create_assembly_callable(u_rec,
                                                     tensor=u,
                                                     form_compiler_parameters=self.cxt.fc_params)

        sigma_rec = A.inv * (g - B * AssembledVector(u) - K_0.T * lambdar)
        self._elim_unknown = create_assembly_callable(sigma_rec,
                                                      tensor=sigma,
                                                      form_compiler_parameters=self.cxt.fc_params)

    @timed_function("HybridRecon")
    def _reconstruct(self):
        """Reconstructs the system unknowns using the multipliers.
        Note that the reconstruction calls are assumed to be
        initialized at this point.
        """
        # We assemble the unknown which is an expression
        # of the first eliminated variable.
        self._sub_unknown()
        # Recover the eliminated unknown
        self._elim_unknown()

    @timed_function("HybridUpdate")
    def update(self, pc):
        """Update by assembling into the operator. No need to
        reconstruct symbolic objects.
        """
        self._assemble_S()
        self.S.force_evaluation()

    def apply(self, pc, x, y):
        """We solve the forward eliminated problem for the
        approximate traces of the scalar solution (the multipliers)
        and reconstruct the "broken flux and scalar variable."

        Lastly, we project the broken solutions into the mimetic
        non-broken finite element space.
        """

        with timed_region("HybridBreak"):
            with self.unbroken_residual.dat.vec_wo as v:
                x.copy(v)

            # Transfer unbroken_rhs into broken_rhs
            # NOTE: Scalar space is already "broken" so no need for
            # any projections
            unbroken_scalar_data = self.unbroken_residual.split()[self.pidx]
            broken_scalar_data = self.broken_residual.split()[self.pidx]
            unbroken_scalar_data.dat.copy(broken_scalar_data.dat)

        with timed_region("HybridProject"):
            # Handle the incoming HDiv residual:
            # Solve (approximately) for `g = A.inv * r`, where `A` is the HDiv
            # mass matrix and `r` is the HDiv residual.
            # Once `g` is obtained, we take the inner product against broken
            # HDiv test functions to obtain a broken residual.
            with self.unbroken_residual.split()[self.vidx].dat.vec_ro as r:
                with self._primal_r.dat.vec_wo as g:
                    self.hdiv_mass_ksp.solve(r, g)

        with timed_region("HybridRHS"):
            # Now assemble the new "broken" hdiv residual
            self._assemble_broken_r()

            # Compute the rhs for the multiplier system
            self._assemble_Srhs()

        with timed_region("HybridSolve"):
            # Solve the system for the Lagrange multipliers
            with self.schur_rhs.dat.vec_ro as b:
                if self.trace_ksp.getInitialGuessNonzero():
                    acc = self.trace_solution.dat.vec
                else:
                    acc = self.trace_solution.dat.vec_wo
                with acc as x_trace:
                    self.trace_ksp.solve(b, x_trace)

        # Reconstruct the unknowns
        self._reconstruct()

        with timed_region("HybridRecover"):
            # Project the broken solution into non-broken spaces
            broken_pressure = self.broken_solution.split()[self.pidx]
            unbroken_pressure = self.unbroken_solution.split()[self.pidx]
            broken_pressure.dat.copy(unbroken_pressure.dat)

            # Compute the hdiv projection of the broken hdiv solution
            if self._method == 'average':
                # The projection is performed using averaging
                # rather than the standard Galerkin projection.
                self.averager.project()
            else:
                self._assemble_projection_rhs()
                with self._projection_rhs.dat.vec_ro as b_proj:
                    with self.unbroken_solution.split()[self.vidx].dat.vec_wo as sol:
                        self.hdiv_projection_ksp.solve(b_proj, sol)

            with self.unbroken_solution.dat.vec_ro as v:
                v.copy(y)

    def applyTranspose(self, pc, x, y):
        """Apply the transpose of the preconditioner."""
        raise NotImplementedError("The transpose application is not implemented.")

    def view(self, pc, viewer=None):
        """Viewer calls for the various configurable objects in this PC."""
        super(HybridizationPC, self).view(pc, viewer)
        viewer.pushASCIITab()
        viewer.printfASCII("Constructing the broken HDiv residual.\n")
        viewer.printfASCII("KSP solver for computing the primal map g = A.inv * r:\n")
        self.hdiv_mass_ksp.view(viewer)
        viewer.popASCIITab()
        viewer.printfASCII("Solving K * A^-1 * K.T using local eliminations.\n")
        viewer.printfASCII("KSP solver for the multipliers:\n")
        viewer.pushASCIITab()
        self.trace_ksp.view(viewer)
        viewer.popASCIITab()
        viewer.printfASCII("Locally reconstructing the broken solutions from the multipliers.\n")
        viewer.pushASCIITab()
        if self._method == 'average':
            viewer.printfASCII("Projecting HDiv solution using the average method.\n")
        else:
            viewer.printfASCII("Projecting the broken HDiv solution into the HDiv space.\n")
            viewer.printfASCII("KSP for the HDiv projection stage:\n")
            viewer.pushASCIITab()
            self.hdiv_projection_ksp.view(viewer)
        viewer.popASCIITab()


def create_schur_nullspace(P, forward, V, V_d, TraceSpace, comm):
    """Gets the nullspace vectors corresponding to the Schur complement
    system for the multipliers.

    :arg P: The mixed operator from the ImplicitMatrixContext.
    :arg forward: A Slate expression denoting the forward elimination
                  operator.
    :arg V: The original "unbroken" space.
    :arg V_d: The broken space.
    :arg TraceSpace: The space of approximate traces.

    Returns: A nullspace (if there is one) for the Schur-complement system.
    """
    from firedrake import assemble, Function, project, AssembledVector

    nullspace = P.getNullSpace()
    if nullspace.handle == 0:
        # No nullspace
        return None

    vecs = nullspace.getVecs()
    tmp = Function(V)
    tmp_b = Function(V_d)
    tnsp_tmp = Function(TraceSpace)
    forward_action = forward * AssembledVector(tmp_b)
    new_vecs = []
    for v in vecs:
        with tmp.dat.vec_wo as t:
            v.copy(t)

        project(tmp, tmp_b)
        assemble(forward_action, tensor=tnsp_tmp)
        with tnsp_tmp.dat.vec_ro as v:
            new_vecs.append(v.copy())

    # Normalize
    for v in new_vecs:
        v.normalize()
    schur_nullspace = PETSc.NullSpace().create(vectors=new_vecs, comm=comm)

    return schur_nullspace
