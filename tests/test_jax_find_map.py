import numpy as np
import pymc as pm
import pytensor.tensor as pt
import pytest

from pymc_experimental.inference.find_map import (
    find_MAP,
    scipy_optimize_funcs_from_loss,
)

pytest.importorskip("jax")


@pytest.fixture(scope="session")
def rng():
    seed = sum(map(ord, "test_fit_map"))
    return np.random.default_rng(seed)


@pytest.mark.parametrize("use_jax_gradients", [True, False], ids=["jax_grad", "pt_grad"])
def test_jax_functions_from_graph(use_jax_gradients):
    x = pt.tensor("x", shape=(2,))

    def compute_z(x):
        z1 = x[0] ** 2 + 2
        z2 = x[0] * x[1] + 3
        return z1, z2

    z = pt.stack(compute_z(x))
    f_loss, f_hess, f_hessp = scipy_optimize_funcs_from_loss(
        loss=z.sum(),
        inputs=[x],
        initial_point_dict={"x": np.array([1.0, 2.0])},
        use_grad=True,
        use_hess=True,
        use_hessp=True,
        use_jax_gradients=use_jax_gradients,
        compile_kwargs=dict(mode="JAX"),
    )

    x_val = np.array([1.0, 2.0])
    expected_z = sum(compute_z(x_val))

    z_jax, grad_val = f_loss(x_val)
    np.testing.assert_allclose(z_jax, expected_z)
    np.testing.assert_allclose(grad_val.squeeze(), np.array([2 * x_val[0] + x_val[1], x_val[0]]))

    hess_val = np.array(f_hess(x_val))
    np.testing.assert_allclose(hess_val.squeeze(), np.array([[2, 1], [1, 0]]))

    hessp_val = np.array(f_hessp(x_val, np.array([1.0, 0.0])))
    np.testing.assert_allclose(hessp_val.squeeze(), np.array([2, 1]))


@pytest.mark.parametrize(
    "method, use_grad, use_hess",
    [
        ("nelder-mead", False, False),
        ("powell", False, False),
        ("CG", True, False),
        ("BFGS", True, False),
        ("L-BFGS-B", True, False),
        ("TNC", True, False),
        ("SLSQP", True, False),
        ("dogleg", True, True),
        ("trust-ncg", True, True),
        ("trust-exact", True, True),
        ("trust-krylov", True, True),
        ("trust-constr", True, True),
    ],
)
@pytest.mark.parametrize("use_jax_gradients", [True, False], ids=["jax_grad", "pt_grad"])
def test_JAX_map(method, use_grad, use_hess, use_jax_gradients, rng):
    extra_kwargs = {}
    if method == "dogleg":
        # HACK -- dogleg requires that the hessian of the objective function is PSD, so we have to pick a point
        # where this is true
        extra_kwargs = {"initvals": {"mu": 2, "sigma_log__": 1}}

    with pm.Model() as m:
        mu = pm.Normal("mu")
        sigma = pm.Exponential("sigma", 1)
        pm.Normal("y_hat", mu=mu, sigma=sigma, observed=rng.normal(loc=3, scale=1.5, size=100))

        optimized_point = find_MAP(
            method=method,
            **extra_kwargs,
            use_grad=use_grad,
            use_hess=use_hess,
            progressbar=False,
            use_jax_gradients=use_jax_gradients,
            compile_kwargs={"mode": "JAX"},
        )
    mu_hat, log_sigma_hat = optimized_point["mu"], optimized_point["sigma_log__"]

    assert np.isclose(mu_hat, 3, atol=0.5)
    assert np.isclose(np.exp(log_sigma_hat), 1.5, atol=0.5)