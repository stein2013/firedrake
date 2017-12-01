import pytest
import numpy as np

from firedrake import *


def test_constant_one_tensor():
    mesh = ExtrudedMesh(UnitIntervalMesh(5), 5)
    one = Constant(1, domain=mesh)
    assert np.allclose(assemble(Tensor(one * dx)), 1.0)
    # Checking to make sure an 'empty' index DTRT for scalars
    assert np.allclose(assemble(Tensor(one * dx)[()]), 1.0)


if __name__ == '__main__':
    import os
    pytest.main(os.path.abspath(__file__))
