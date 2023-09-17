import re
import sys

import numpy as np
import pymc as pm
import pytensor
import pytensor.tensor as pt
import pytest
from numpy.testing import assert_allclose
from scipy.stats import multivariate_normal

from pymc_experimental.statespace import structural
from pymc_experimental.statespace.filters.distributions import LinearGaussianStateSpace
from pymc_experimental.statespace.utils.constants import (
    ALL_STATE_DIM,
    OBS_STATE_DIM,
    TIME_DIM,
)
from pymc_experimental.tests.statespace.utilities.shared_fixtures import (  # pylint: disable=unused-import
    rng,
)
from pymc_experimental.tests.statespace.utilities.test_helpers import (
    delete_rvs_from_model,
    fast_eval,
    load_nile_test_data,
)

floatX = pytensor.config.floatX

# TODO: These are pretty loose because of all the stabilizing of covariance matrices that is done inside the kalman
#  filters. When that is improved, this should be tightened.
ATOL = 1e-6 if floatX.endswith("64") else 1e-4
RTOL = 1e-6 if floatX.endswith("64") else 1e-4

filter_names = [
    "standard",
    "cholesky",
    "univariate",
    "single",
    "steady_state",
]


@pytest.fixture(scope="session")
def data():
    return load_nile_test_data()


@pytest.fixture(scope="session")
def pymc_model(data):
    with pm.Model() as mod:
        data = pm.ConstantData("data", data.values)
        P0_diag = pm.Exponential("P0_diag", 1, shape=(2,))
        P0 = pm.Deterministic("P0", pt.diag(P0_diag))
        initial_trend = pm.Normal("initial_trend", shape=(2,))
        sigma_trend = pm.Exponential("sigma_trend", 1, shape=(2,))

    return mod


@pytest.fixture(scope="session")
def pymc_model_2(data):
    coords = {
        ALL_STATE_DIM: ["level", "trend"],
        OBS_STATE_DIM: ["level"],
        TIME_DIM: np.arange(101, dtype="int"),
    }

    with pm.Model(coords=coords) as mod:
        P0_diag = pm.Exponential("P0_diag", 1, shape=(2,))
        P0 = pm.Deterministic("P0", pt.diag(P0_diag))
        initial_trend = pm.Normal("initial_trend", shape=(2,))
        sigma_trend = pm.Exponential("sigma_trend", 1, shape=(2,))
        sigma_me = pm.Exponential("sigma_error", 1)

    return mod


@pytest.fixture(scope="session")
def ss_mod_me():
    ss_mod = structural.LevelTrendComponent(order=2)
    ss_mod += structural.MeasurementError(name="error")
    ss_mod = ss_mod.build("data", verbose=False)

    return ss_mod


@pytest.fixture(scope="session")
def ss_mod_no_me():
    ss_mod = structural.LevelTrendComponent(order=2)
    ss_mod = ss_mod.build("data", verbose=False)

    return ss_mod


@pytest.mark.parametrize("kfilter", filter_names, ids=filter_names)
def test_loglike_vectors_agree(kfilter, pymc_model):
    ss_mod = structural.LevelTrendComponent(order=2).build(
        "data", verbose=False, filter_type=kfilter
    )
    with pymc_model:
        ss_mod._insert_random_variables()
        matrices = ss_mod.unpack_statespace()

        filter_outputs = ss_mod.kalman_filter.build_graph(pymc_model["data"], *matrices)
        filter_mus, pred_mus, obs_mu, filter_covs, pred_covs, obs_cov, ll = filter_outputs

    test_ll = fast_eval(ll)

    # TODO: BUG: Why does fast eval end up with a 2d output when filter is "single"?
    obs_mu_np = obs_mu.eval()
    obs_cov_np = fast_eval(obs_cov)
    data_np = fast_eval(pymc_model["data"])

    scipy_lls = []
    for y, mu, cov in zip(data_np, obs_mu_np, obs_cov_np):
        scipy_lls.append(multivariate_normal.logpdf(y, mean=mu, cov=cov))
    assert_allclose(test_ll, np.array(scipy_lls).ravel(), atol=ATOL, rtol=RTOL)


def test_lgss_distribution_from_steps(ss_mod_me, pymc_model_2):
    with pymc_model_2:
        ss_mod_me._insert_random_variables()
        matrices = ss_mod_me.unpack_statespace()

        # pylint: disable=unpacking-non-sequence
        latent_states, obs_states = LinearGaussianStateSpace("states", *matrices, steps=100)
        # pylint: enable=unpacking-non-sequence

        idata = pm.sample_prior_predictive(samples=10)
        delete_rvs_from_model(["states_latent", "states_observed", "states_combined"])

    assert idata.prior.coords["states_latent_dim_0"].shape == (101,)

    for output_name in ["states_latent", "states_observed"]:
        assert not np.any(np.isnan(idata.prior[output_name].values))


def test_lgss_distribution_specify_k_endog(ss_mod_me, pymc_model_2):
    with pymc_model_2:
        ss_mod_me._insert_random_variables()
        matrices = ss_mod_me.unpack_statespace()

        # pylint: disable=unpacking-non-sequence
        latent_states_1, obs_states_1 = LinearGaussianStateSpace(
            "states_1", *matrices, steps=100, k_endog=1
        )
        latent_states_2, obs_states_2 = LinearGaussianStateSpace("states_2", *matrices, steps=100)
        # pylint: enable=unpacking-non-sequence

        idata = pm.sample_prior_predictive(samples=10)
        delete_rvs_from_model(
            [
                "states_1_latent",
                "states_1_observed",
                "states_1_combined",
                "states_2_latent",
                "states_2_observed",
                "states_2_combined",
            ]
        )

    assert idata.prior["states_1_latent"].shape == idata.prior["states_2_latent"].shape
    assert idata.prior["states_1_observed"].shape == idata.prior["states_2_observed"].shape


def test_lgss_raises_on_wrong_k_endog(ss_mod_me, pymc_model_2):
    with pymc_model_2:
        ss_mod_me._insert_random_variables()
        matrices = ss_mod_me.unpack_statespace()

        with pytest.raises(
            ValueError,
            match=re.escape("Inferred k_endog does not agree with provided value (1 != 2)."),
        ):
            # pylint: disable=unpacking-non-sequence
            latent_states, obs_states = LinearGaussianStateSpace(
                "states_1", *matrices, steps=100, k_endog=2
            )
            # pylint: enable=unpacking-non-sequence


def test_lgss_distribution_with_dims(ss_mod_me, pymc_model_2):
    with pymc_model_2:
        ss_mod_me._insert_random_variables()
        matrices = ss_mod_me.unpack_statespace()

        # pylint: disable=unpacking-non-sequence
        latent_states, obs_states = LinearGaussianStateSpace(
            "states", *matrices, steps=100, dims=[TIME_DIM, ALL_STATE_DIM, OBS_STATE_DIM]
        )
        # pylint: enable=unpacking-non-sequence
        idata = pm.sample_prior_predictive(samples=10)
        delete_rvs_from_model(["states_latent", "states_observed", "states_combined"])

    assert idata.prior.coords["time"].shape == (101,)
    assert all(
        [dim in idata.prior.states_latent.coords.keys() for dim in [TIME_DIM, ALL_STATE_DIM]]
    )
    assert all(
        [dim in idata.prior.states_observed.coords.keys() for dim in [TIME_DIM, OBS_STATE_DIM]]
    )

    for output_name in ["states_latent", "states_observed"]:
        assert not np.any(np.isnan(idata.prior[output_name].values)), output_name


def test_lgss_with_time_varying_inputs(rng):
    X = rng.random(size=(10, 3), dtype=floatX)
    ss_mod = structural.LevelTrendComponent() + structural.RegressionComponent(
        name="exog", k_exog=3
    )
    mod = ss_mod.build("data", verbose=False)

    coords = {
        ALL_STATE_DIM: ["level", "trend", "beta_1", "beta_2", "beta_3"],
        OBS_STATE_DIM: ["level"],
        TIME_DIM: np.arange(10, dtype="int"),
    }

    with pm.Model(coords=coords):
        exog_data = pm.MutableData("data_exog", X)
        P0_diag = pm.Exponential("P0_diag", 1, shape=(mod.k_states,))
        P0 = pm.Deterministic("P0", pt.diag(P0_diag))
        initial_trend = pm.Normal("initial_trend", shape=(2,))
        sigma_trend = pm.Exponential("sigma_trend", 1, shape=(2,))
        beta_exog = pm.Normal("beta_exog", shape=(3,))

        intercept = pm.Normal("intercept")
        slope = pm.Normal("slope")
        trend = intercept + slope * pt.arange(10, dtype=floatX)
        mod.add_exogenous(trend)

        mod._insert_random_variables()
        matrices = mod.unpack_statespace()

        # pylint: disable=unpacking-non-sequence
        latent_states, obs_states = LinearGaussianStateSpace(
            "states",
            *matrices,
            steps=9,
            sequence_names=["d", "Z"],
            dims=[TIME_DIM, ALL_STATE_DIM, OBS_STATE_DIM]
        )
        # pylint: enable=unpacking-non-sequence
        idata = pm.sample_prior_predictive(samples=10)

    assert idata.prior.coords["time"].shape == (10,)
    assert all(
        [dim in idata.prior.states_latent.coords.keys() for dim in [TIME_DIM, ALL_STATE_DIM]]
    )
    assert all(
        [dim in idata.prior.states_observed.coords.keys() for dim in [TIME_DIM, OBS_STATE_DIM]]
    )

    for output_name in ["states_latent", "states_observed"]:
        assert not np.any(np.isnan(idata.prior[output_name].values)), output_name


@pytest.mark.skipif(sys.platform == "win32", reason="JAX not supported on windows.")
def test_lgss_with_degenerate_covariance_jax(rng):
    # In pytensor/pymc, draws from MvN(0, 0) are 0.0, but in JAX they are nan. This test checks that the degenerate
    # case is covered in JAX mode.

    X = rng.random(size=(10, 3), dtype=floatX)
    ss_mod = structural.LevelTrendComponent(innovations_order=0) + structural.TimeSeasonality(
        name="seasonal", season_length=3, state_names=["A", "B", "C"], innovations=False
    )
    mod = ss_mod.build("data", verbose=False)

    coords = {
        ALL_STATE_DIM: ["level", "trend", "B", "C"],
        OBS_STATE_DIM: ["data"],
        TIME_DIM: np.arange(10, dtype="int"),
    }

    with pm.Model(coords=coords) as m:
        exog_data = pm.MutableData("data_exog", X)

        P0_diag = pm.Exponential("P0_diag", 1, shape=(mod.k_states,))
        P0 = pm.Deterministic("P0", pt.diag(P0_diag))
        initial_trend = pm.Normal("initial_trend", shape=(2,))
        seasonal_coefs = pm.Normal("seasonal_coefs", shape=(2,))

        mod._insert_random_variables()
        matrices = mod.unpack_statespace()

        # pylint: disable=unpacking-non-sequence
        latent_states, obs_states = LinearGaussianStateSpace(
            "states", *matrices, steps=9, dims=[TIME_DIM, ALL_STATE_DIM, OBS_STATE_DIM], mode="JAX"
        )
        # pylint: enable=unpacking-non-sequence
        idata = pm.sample_prior_predictive(samples=10, compile_kwargs={"mode": "JAX"})

    for output_name in ["states_latent", "states_observed"]:
        assert not np.any(np.isnan(idata.prior[output_name].values)), output_name
