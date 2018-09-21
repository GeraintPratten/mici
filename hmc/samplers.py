"""Hamiltonian Monte Carlo MCMC sampler classes."""

import logging
import numpy as np
from hmc.utils import LogRepFloat
from hmc.integrators import IntegratorError
try:
    import tqdm
    TQDM_AVAILABLE = True
except ImportError:
    TQDM_AVAILABLE = False

logger = logging.getLogger(__name__)


def extract_pos(state):
    """Helper function to extract position from chain state."""
    return state.pos


def extract_mom(state):
    """Helper function to extract momentum from chain state."""
    return state.mom


class BaseHamiltonianMonteCarlo(object):
    """Abstract base class for Hamiltonian Monte Carlo (HMC) implementations.

    Here a HMC implementation is considered a MCMC method which augments the
    original target state space with a momentum variable with a user specified
    conditional distribution given the target variable. In each chain iteration
    two Markov transitions leaving the resulting joint target distribution
    invariant are applied - the momentum variables are independently resampled
    from their conditional distribution in a Gibbs sampling step and then a
    trajectory in the joint space is generated by simulating a Hamiltonian
    dynamic with an appropriate symplectic integrator which is exactly
    reversible, volume preserving and approximately conserves the joint
    probability density of the target-momentum state pair. One state from the
    resulting trajectory is then selected as the next joint chain state using
    an appropriate sampling scheme such that the joint distribution is left
    exactly invariant.

    References:

      1. Duane, S., Kennedy, A.D., Pendleton, B.J. and Roweth, D., 1987.
         Hybrid Monte Carlo. Physics letters B, 195(2), pp.216-222.
      2. Neal, R.M., 2011. MCMC using Hamiltonian dynamics.
         Handbook of Markov Chain Monte Carlo, 2(11), p.2.
    """

    def __init__(self, system, integrator, rng):
        """
        Args:
            system: Hamiltonian system to be simulated.
            integrator: Symplectic integrator appropriate to the specified
                Hamiltonian system.
            rng: Numpy RandomState random number generator instance.
        """
        self.system = system
        self.integrator = integrator
        self.rng = rng

    def sample_momentum_transition(self, state):
        state.mom = self.system.sample_momentum(state, self.rng)
        return state

    def sample_dynamics_transition(self, state):
        raise NotImplementedError()

    def initialise_chain_stats(self, init_state, n_sample):
        raise NotImplementedError()

    def update_chain_stats(self, s, chain_stats, trans_stats):
        raise NotImplementedError()

    def sample_chain(self, n_sample, state, chain_var_funcs=[extract_pos]):
        if state.mom is None:
            state.mom = self.system.sample_momentum(state, self.rng)
        chain_stats = self.initialise_chain_stats(state, n_sample)
        var_chains = []
        for chain_func in chain_var_funcs:
            var = chain_func(state)
            var_chains.append(np.empty((n_sample,) + var.shape))
            var_chains[-1][0] = var
        if TQDM_AVAILABLE:
            s_range = tqdm.trange(
                n_sample - 1, desc='Running chain', unit='it')
        else:
            s_range = range(n_sample - 1)
        for s in s_range:
            state = self.sample_momentum_transition(state)
            state, trans_stats = self.sample_dynamics_transition(state)
            self.update_chain_stats(s + 1, chain_stats, trans_stats)
            for chain_func, var_chain in zip(chain_var_funcs, var_chains):
                var_chain[s + 1] = chain_func(state)
        return var_chains + [chain_stats]


class BaseMetropolisHMC(BaseHamiltonianMonteCarlo):
    """Base for HMC methods using a Metropolis accept step to sample new state.

    In each transition a trajectory is generated by integrating the Hamiltonian
    dynamics from the current state in the current integration time direction
    for a number of integrator steps.

    The state at the end of the trajectory with the integration direction
    negated (this ensuring the proposed move is an involution) is used as the
    proposal in a Metropolis acceptance step. The integration direction is then
    deterministically negated again irrespective of the accept decision, with
    the effect being that on acceptance the integration direction will be equal
    to its initial value and on rejection the integration direction will be
    the negation of its initial value.
    """

    def initialise_chain_stats(self, init_state, n_sample):
        chain_stats = {
            'hamiltonian': np.empty(n_sample, np.float64),
            'n_step': np.empty(n_sample - 1, np.int64),
            'accept_prob': np.empty(n_sample - 1, np.float64)
        }
        chain_stats['hamiltonian'][0] = self.system.h(init_state)
        return chain_stats

    def update_chain_stats(self, s, chain_stats, trans_stats):
        chain_stats['hamiltonian'][s] = trans_stats['hamiltonian']
        chain_stats['n_step'][s - 1] = trans_stats['n_step']
        chain_stats['accept_prob'][s - 1] = trans_stats['accept_prob']

    def _sample_dynamics_transition(self, state, n_step):
        h_init = self.system.h(state)
        state_p = state
        try:
            for s in range(n_step):
                state_p = self.integrator.step(state_p)
        except RuntimeError as e:
            logger.warning(
                f'Terminating trajectory due to integrator error: {e!s}')
            return state, {
                'hamiltonian': h_init, 'accept_prob': 0, 'n_step': s}
        state_p.dir *= -1
        h_final = self.system.h(state_p)
        accept_prob = min(1, np.exp(h_init - h_final))
        if self.rng.uniform() < accept_prob:
            state = state_p
        state.dir *= -1
        stats = {'hamiltonian': self.system.h(state),
                 'accept_prob': accept_prob, 'n_step': n_step}
        return state, stats


class StaticMetropolisHMC(BaseMetropolisHMC):
    """Static integration time HMC implementation with Metropolis sampling.

    In this variant the trajectory is generated by integrating the state
    through time a fixed number of integrator steps. This is original proposed
    Hybrid Monte Carlo (often now instead termed Hamiltonian Monte Carlo)
    algorithm [1,2].

    References:

      1. Duane, S., Kennedy, A.D., Pendleton, B.J. and Roweth, D., 1987.
         Hybrid Monte Carlo. Physics letters B, 195(2), pp.216-222.
      2. Neal, R.M., 2011. MCMC using Hamiltonian dynamics.
         Handbook of Markov Chain Monte Carlo, 2(11), p.2.
    """

    def __init__(self, system, integrator, rng, n_step):
        super().__init__(system, integrator, rng)
        self.n_step = n_step

    def sample_dynamics_transition(self, state):
        return self._sample_dynamics_transition(state, self.n_step)


class RandomMetropolisHMC(BaseMetropolisHMC):
    """Random integration time HMC with Metropolis sampling of new state.

    In each dynamics transition a trajectory is generated by integrating the
    state in the current integration direction in time a random integer number
    of integrator steps sampled from the uniform distribution on an integer
    interval. The randomisation of the number of integration steps avoids the
    potential of the chain mixing poorly due to using an integration time close
    to the period of (near) periodic systems [1,2].

    References:

      1. Neal, R.M., 2011. MCMC using Hamiltonian dynamics.
         Handbook of Markov Chain Monte Carlo, 2(11), p.2.
      2. Mackenzie, P.B., 1989. An improved hybrid Monte Carlo method.
         Physics Letters B, 226(3-4), pp.369-371.
    """

    def __init__(self, system, integrator, rng, n_step_range):
        super().__init__(system, integrator, rng)
        n_step_lower, n_step_upper = n_step_range
        assert n_step_lower > 0 and n_step_lower < n_step_upper
        self.n_step_range = n_step_range

    def sample_dynamics_transition(self, state):
        n_step = self.rng.random_integers(*self.n_step_range)
        return self._sample_dynamics_transition(state, n_step)


class BaseCorrelatedMomentumHMC(BaseHamiltonianMonteCarlo):
    """Base class for HMC methods using correlated (partial) momentum updates.

    Rather than independently sampling a new momentum on each chain iteration,
    instances of derived classes instead pertubatively update the momentum with
    a Crank-Nicolson type update which produces a new momentum value with a
    specified correlation with the previous value. It is assumed that the
    conditional distribution of the momenta is zero-mean Gaussian such that the
    Crank-Nicolson update leaves the momenta conditional distribution exactly
    invariant. This approach is sometimes known as partial momentum refreshing
    or updating, and was originally proposed in [1].

    If the resampling coefficient is equal to one then the momentum is not
    randomised at all and successive dynamics transitions will continue along
    the same simulated Hamiltonian trajectory. When a transition is accepted
    this means the subsequent simulated trajectory will continue evolving in
    the same direction and so not randomising the momentum will reduce random
    walk behaviour. However on a rejection the integration direction is
    reversed and so without randomisation the trajectory will exactly backtrack
    along the previous tractory states. A correlation coefficient of zero
    corresponds to the standard case of independent resampling of the momenta
    while intermediate values between zero and one correspond to varying levels
    of correlation between the pre and post update momentums.

    References:

      1. Horowitz, A.M., 1991. A generalized guided Monte Carlo algorithm.
         Phys. Lett. B, 268(CERN-TH-6172-91), pp.247-252.
    """

    def __init__(self, system, integrator, rng, mom_resample_coeff=1.):
        super().__init__(system, integrator, rng)
        self.mom_resample_coeff = mom_resample_coeff

    def sample_momentum_transition(self, state):
        return self.system.sample_momentum(state, self.rng)
        if self.mom_resample_coeff == 1:
            state.mom = self.system.sample_momentum(state, self.rng)
        elif self.mom_resample_coeff != 0:
            mom_ind = self.system.sample_momentum(state, self.rng)
            state.mom *= (1. - self.mom_resample_coeff**2)**0.5
            state.mom += self.mom_resample_coeff * mom_ind
        return state


class StaticMetropolisCorrelatedMomentumHMC(
        BaseCorrelatedMomentumHMC, StaticMetropolisHMC):
    """Static integration time Metropolis HMC with correlated momentum updates.

    See StaticMetropolisHMC and BaseCorrelatedMomentumHMC class docstrings for
    full details. Correlated momentum updates assume a zero-mean Gaussian
    conditional distribution on the momenta.
    """


class RandomMetropolisCorrelatedMomentumHMC(
        BaseCorrelatedMomentumHMC, RandomMetropolisHMC):
    """Random integration time Metropolis HMC with correlated momentum updates.

    See RandomMetropolisHMC and BaseCorrelatedMomentumHMC class docstrings for
    full details. Correlated momentum updates assume a zero-mean Gaussian
    conditional distribution on the momenta.
    """


class DynamicMultinomialHMC(BaseHamiltonianMonteCarlo):
    """Dynamic integration time HMC with multinomial sampling of new state.

    In each transition a binary tree of states is recursively computed by
    integrating randomly forward and backward in time by a number of steps
    equal to the previous tree size [1,2] until a termination criteria on the
    tree leaves is met [1,3]. The next chain state is chosen from the candidate
    states using a progressive multinomial resampling scheme [2] based on the
    relative probability densities of the different candidate states, with the
    resampling biased towards states further from the current state.

    References:

      1. Hoffman, M.D. and Gelman, A., 2014. The No-U-turn sampler:
         adaptively setting path lengths in Hamiltonian Monte Carlo.
         Journal of Machine Learning Research, 15(1), pp.1593-1623.
      2. Betancourt, M., 2017. A conceptual introduction to Hamiltonian Monte
         Carlo. arXiv preprint arXiv:1701.02434.
      3. Betancourt, M., 2013. Generalizing the no-U-turn sampler to Riemannian
         manifolds. arXiv preprint arXiv:1304.1920.
    """

    def __init__(self, system, integrator, rng, max_tree_depth=5,
                 max_delta_h=1000):
        super().__init__(system, integrator, rng)
        self.max_tree_depth = max_tree_depth
        self.max_delta_h = max_delta_h

    def initialise_chain_stats(self, init_state, n_sample):
        chain_stats = {
            'hamiltonian': np.empty(n_sample, np.float64),
            'n_step': np.empty(n_sample - 1, np.int64),
            'accept_prob': np.empty(n_sample - 1, np.float64),
            'tree_depth': np.empty(n_sample - 1, np.int64),
            'divergent': np.empty(n_sample - 1, np.bool)
        }
        chain_stats['hamiltonian'][0] = self.system.h(init_state)
        return chain_stats

    def update_chain_stats(self, s, chain_stats, trans_stats):
        chain_stats['hamiltonian'][s] = trans_stats['hamiltonian']
        chain_stats['n_step'][s - 1] = trans_stats['n_step']
        chain_stats['accept_prob'][s - 1] = trans_stats['accept_prob']
        chain_stats['tree_depth'][s - 1] = trans_stats['tree_depth']
        chain_stats['divergent'][s - 1] = trans_stats['divergent']

    def termination_criterion(self, state_1, state_2, sum_mom):
        return (
            self.system.dh_dmom(state_1).dot(sum_mom) < 0 or
            self.system.dh_dmom(state_2).dot(sum_mom) < 0)

    # Key to subscripts used in build_tree and sample_dynamics_transition
    # _p : proposal
    # _n : next
    # _l : left (negative direction)
    # _r : right (positive direction)
    # _s : subtree
    # _i : inner subsubtree
    # _o : outer subsubtree

    def build_tree(self, depth, state, sum_mom, sum_weight, stats, h_init):
        if depth == 0:
            # recursion base case
            try:
                state = self.integrator.step(state)
                hamiltonian = self.system.h(state)
                sum_mom += state.mom
                sum_weight += LogRepFloat(log_val=-hamiltonian)
                stats['sum_acc_prob'] += min(1., np.exp(h_init - hamiltonian))
                stats['n_step'] += 1
                terminate = hamiltonian - h_init > self.max_delta_h
                if terminate:
                    stats['divergent'] = True
                    logger.warning(
                        f'Terminating build_tree due to integrator divergence '
                        f'(delta_h = {hamiltonian - h_init:.1e}).')
            except RuntimeError as e:
                logger.warning(
                    f'Terminating build_tree due to integrator error: {e!s}')
                state = None
                terminate = True
            return terminate, state, state, state
        sum_mom_i, sum_mom_o = np.zeros((2, state.n_dim))
        sum_weight_i, sum_weight_o = LogRepFloat(0.), LogRepFloat(0.)
        # build inner subsubtree
        terminate_i, state_i, state, state_pi = self.build_tree(
            depth - 1, state, sum_mom_i, sum_weight_i, stats, h_init)
        if terminate_i:
            return True, None, None, None
        # build outer subsubtree
        terminate_o, _, state_o, state_po = self.build_tree(
            depth - 1, state, sum_mom_o, sum_weight_o, stats, h_init)
        if terminate_o:
            return True, None, None, None
        # independently sample proposal from 2 subsubtrees by relative weights
        sum_weight_s = sum_weight_i + sum_weight_o
        accept_o_prob = sum_weight_o / sum_weight_s
        state_p = state_po if self.rng.uniform() < accept_o_prob else state_pi
        # update overall tree weight
        sum_weight += sum_weight_s
        # calculate termination criteria for subtree
        sum_mom_s = sum_mom_i + sum_mom_o
        terminate_s = self.termination_criterion(state_i, state_o, sum_mom_s)
        # update overall tree summed momentum
        sum_mom += sum_mom_s
        return terminate_s, state_i, state_o, state_p

    def sample_dynamics_transition(self, state):
        h_init = self.system.h(state)
        sum_mom = state.mom.copy()
        sum_weight = LogRepFloat(log_val=-h_init)
        stats = {'n_step': 0, 'divergent': False, 'sum_acc_prob': 0.}
        state_n, state_l, state_r = state, state.copy(), state.copy()
        # set integration directions of initial left and right tree leaves
        state_l.dir = -1
        state_r.dir = +1
        for depth in range(self.max_tree_depth):
            # uniformly sample direction to expand tree in
            direction = 2 * (self.rng.uniform() < 0.5) - 1
            sum_mom_s = np.zeros(state.n_dim)
            sum_weight_s = LogRepFloat(0.)
            if direction == 1:
                # expand tree by adding subtree to right edge
                terminate_s, _, state_r, state_p = self.build_tree(
                    depth, state_r, sum_mom_s, sum_weight_s, stats, h_init)
            else:
                # expand tree by adding subtree to left edge
                terminate_s, _, state_l, state_p = self.build_tree(
                    depth, state_l, sum_mom_s, sum_weight_s, stats, h_init)
            if terminate_s:
                break
            # progressively sample new state by choosing between
            # current new state and proposal from new subtree, biasing
            # towards the new subtree proposal
            if self.rng.uniform() < sum_weight_s / sum_weight:
                state_n = state_p
            sum_weight += sum_weight_s
            sum_mom += sum_mom_s
            if self.termination_criterion(state_l, state_r, sum_mom):
                break
        if stats['n_step'] > 0:
            stats['accept_prob'] = stats['sum_acc_prob'] / stats['n_step']
        else:
            stats['accept_prob'] = 0.
        stats['hamiltonian'] = self.system.h(state_n)
        stats['tree_depth'] = depth
        return state_n, stats
