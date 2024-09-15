from functools import lru_cache
import json
import logging
import math
import numpy as np

from argparse import ArgumentTypeError

import itertools
import ast

from typing import List, Literal, Tuple, TypedDict

from scipy.special import binom
from scipy.optimize import OptimizeResult
from skopt.space import Categorical
from scipy.stats import norm

from protocol_asymmetric import SwapTreeVertex, assign_dists_to_tree, generate_swap_space, generate_dists_combs
from repeater_types import checkAsymProtocol

logging.basicConfig(level=logging.INFO)

# TODO: move these types to repeater_types.py
# Define the exception for the CDF coverage
class ThresholdExceededError(Exception):
    """
    This exception is raised when the CDF coverage is below the threshold.
    """
    def __init__(self, message="CDF under threshold count incremented", extra_info=None):
        super().__init__(message)
        self.extra_info = extra_info

# Define a type for the parameters
class SimParameters(TypedDict):
    protocol: Tuple[int]
    t_coh: float
    p_gen: float
    p_swap: float
    w0: float

# Define the type for the optimizer
OptimizerType = Literal["bf", "gp"]

# Validation function for the optimizer type
def optimizerType(value: str) -> OptimizerType:
    valid_options = ("gp", "bf")
    if value not in valid_options:
        raise ArgumentTypeError(f"Invalid optimizer type: {value}. Available options are: {', '.join(valid_options)}")
    return value

# Define the type for space_type
SpaceType = Literal["one_level", "strategy", "enumerate", "centerspace", "asymmetric"]

# Validation function for the space type
def spaceType(value: str) -> SpaceType:
    # TODO: refactor: valid_options = SpaceType.__args__
    valid_options = ("one_level", "strategy", "enumerate", "centerspace", "asymmetric")
    if value not in valid_options:
        raise ArgumentTypeError(f"Invalid space type: {value}. Available options are: {', '.join(valid_options)}")
    return value


def index_lowercase_alphabet(i):
    """
    Takes in input an integer i and returns the corresponding lowercase letter in the alphabet.
    """
    return chr(i + 97)


def remove_unstable_werner(pmf, w_func, threshold=1.0e-15):
    """
    Removes unstable Werner parameters where the probability mass is below a specified threshold
    and returns a new Werner parameter array without modifying the input array.

    Parameters:
    - pmf (np.array): The probability mass function array.
    - w_func (np.array): The input Werner parameter array.
    - threshold (float): The threshold below which Werner parameters are considered unstable.
    
    Returns:
    - np.array: A new Werner parameter array with unstable parameters removed.

     note: it is used in plots to remove oscillations in the plot.
    """
    new_w_func = w_func.copy()
    for t in range(len(pmf)):
        if pmf[t] < threshold:
            new_w_func[t] = np.nan
    return new_w_func


def get_all_maxima(ordered_results: List[Tuple[np.float64, Tuple[int]]], min_dists, max_dists) -> List[Tuple[np.float64, Tuple[int]]]:
    """
    Get all the maxima from the results: the secret key rate and the protocol
        for the best secret key rate for each distillation number
    """
    maxima = []
    for number_of_dists in range(min_dists, max_dists + 1):
        evaluations = [result for result in ordered_results if result[1].count(1) == number_of_dists]
        # Skip if there are no evaluations for the number of distillations
        if len(evaluations) == 0:
            continue
        maximum = max(evaluations, key=lambda x: x[0])
        # Do not append if the maximum is zero
        if maximum[0] > 0:
            maxima.append(maximum)
    return maxima


def get_protocol_space_size(space_type: SpaceType, min_dists, max_dists, number_of_swaps):
    """
    Returns the total number of protocols to be tested 
        for a specific space type and number of swaps.
    """
    if space_type == "one_level":
        if min_dists == 0:
            return (max_dists - min_dists) * (number_of_swaps + 1) + 1
        else:
            return (max_dists - min_dists + 1) * (number_of_swaps + 1)
    elif space_type == "enumerate" or space_type == "strategy" or space_type == "centerspace":
        return get_no_of_permutations_per_swap(min_dists, max_dists, number_of_swaps)


def get_no_of_permutations_per_swap(min_dists, max_dists, s):
    """
    Returns the total number of permutations to be tested for a fixed number of swaps.
    """
    return int(sum([binom(s + d, d) for d in range(min_dists, max_dists + 1)]))


def get_analytical_permutations(min_dists, max_dists, min_swaps, max_swaps):
    """
    Returns the total number of permutations to be tested.
    """
    total_permutations = 0
    for s in range(min_swaps, max_swaps + 1):
        permutations = get_no_of_permutations_per_swap(s, min_dists, max_dists)
        total_permutations += permutations
    return total_permutations


def get_protocol_enum_space(min_dists, max_dists, number_of_swaps, skopt_space=False):
    """
    The permutation space is used to test all the possible combinations of distillations for a fixed number of swaps.
    For each number of distillation tested, we test all the possible permutations of distillations. 
        i.e. for 2 swaps and from 0 to 2 distillations, we test:
        - zero distillations
            (0, 0), 
        - one distillation
            (0, 0, 1), (0, 1, 0), (1, 0, 0),
        - two distillations
            (0, 0, 1, 1), (0, 1, 0, 1), (0, 1, 1, 0), (1, 0, 0, 1), (1, 0, 1, 0), (1, 1, 0, 0)
    """
    space = []
    for num_dists in range(min_dists, max_dists + 1):
        base = [1] * num_dists + [0] * number_of_swaps
        perm_set = set(itertools.permutations(base))
        space.extend(perm_set)
    
    # Order the space: first by ascending length of the protocol, then by the ascending binary representation of the protocol tuple
    space = sorted(space, key=lambda x: (len(x), -int(''.join(map(str, x)), 2)))

    analytical_permutations = get_no_of_permutations_per_swap(min_dists, max_dists, number_of_swaps)
    assert len(space) == analytical_permutations, f"Expected max. {analytical_permutations} permutations, got {len(space)}"
    
    if skopt_space:
        space = [Categorical([''.join(str(tup)) for tup in space], name='protocol')]
    
    return space


def catalan_number(n):
    """
    Returns the n-th Catalan number.
    """
    return math.comb(2*n, n) // (n + 1)


def get_distillation_per_swap(S: int, max_dists: int) -> int:
    """
    Returns the number of distillations per swap.
    """
    return (max_dists+1)**(2*S-1)

def get_asym_protocol_space_size(nodes: int, max_dists: int) -> int:
    """
    Returns the space of asymmetric protocols to be tested,
     by analytical derivation.
    """
    if nodes < 2:
        raise ValueError("N should be at least 2")
    elif nodes == 2:
        return max_dists + 1
    
    S = nodes - 1

    expected = catalan_number(nodes-2) * get_distillation_per_swap(S, max_dists)
    return expected


def get_asym_protocol_space(nodes: int, max_dists: int) -> List[Tuple[str]]:
    """
    Returns the space of asymmetric protocols to be tested, sorted by length of the protocols.
    For each number of distillation tested, we test all the possible asymmetric protocols.
    TODO: consider ordering the swap space by symmetry score.
    """
    space = []
    S = nodes - 1

    swap_space = get_swap_space(S)
    assert len(swap_space) == catalan_number(nodes-2), f"Expected {catalan_number(nodes-2)} swap sequences, got {len(swap_space)}"

    for idx, (_, swap_tree, swap_sequence) in enumerate(swap_space):
        logging.info(f"{idx+1}/{len(swap_space)} Generating protocols for swap sequence {swap_sequence}...")
        space.extend(get_joined_sequences(S, swap_tree, max_dists))

    expected_protocols = get_asym_protocol_space_size(nodes, max_dists)
    assert len(space) == expected_protocols, \
        f"Expected {get_asym_protocol_space_size(nodes, max_dists)} protocols, got {len(space)}"
    
    space = sorted(space, key=lambda x: (len(x), x))
    return space


def get_joined_sequences(S: int, swap_tree: SwapTreeVertex, max_dists: int) -> List[Tuple[str]]:
    """
    Returns the space of edge combinations for a given swap tree and a maximum number of distillations.
    """
    vertices = 2*S - 1
    space: List[Tuple[str]] = []

    for edgeLabeledShape in generate_dists_combs(vertices, swap_tree, max_dists):
        space.append(edgeLabeledShape.get_sequence())

    return space


@lru_cache(maxsize=None)
def get_swap_space(S: int):
    """
    Returns the swap space for a given number of nodes
        with information about
        - the simmetricity score 
        - the swap tree
        - the swap sequence
    """
    if S < 1:
        raise ValueError("S should be at least 1")
    
    swap_space = []
    for tree, seq in generate_swap_space(S):
        symmetry_score = tree.get_symmetry_score()
        swap_space.append((symmetry_score, tree, seq))

    return swap_space


def sample_asymmetric_protocol_distillations(kappa, mu, sigma, v, beta):
    """
    Generates a sequence of values for the distillation rounds in the segments
      by sampling them from a normal distribution.

    Parameters:
    - kappa (float): The total number of rounds of distillation to be performed among all segments.
    - mu (float): The mean of the distribution from which to sample distillation rounds per segment.
    - sigma (float): The std. dev. of the distribution from which to sample distillation rounds per segment.
    - v (float): The parameter for the total number of rounds of distillation to be performed among all segments.
    - beta (float): The parameter for the simmetricity of the swap tree.
    
    TODO: Increase sigma to avoid infinite loops (maybe a better approach is needed)
    """
    if kappa == 0:
        return []
    
    dists = [0]*v
    
    while sum(dists) < kappa:
        # Generate a sample from the normal distribution
        # Assign out-of-bound values to the closest limit (0 or v-1)
        sample = int(np.round(np.random.normal(mu, sigma)))
        sample = np.clip(sample, 0, v-1)
        
        # Add the sample if it hasn't been chosen more than beta times
        if dists[sample] < beta:
            dists[sample] += 1
        else:
            # Increase sigma to avoid infinite loops
            sigma += 0.01

    return dists


def get_protocol_from_center_spacing_symmetricity(nodes, max_dists, gamma, kappa, zeta, tau):
    """
    Generates a protocol from its parameters.

    Parameters:
    - nodes (int): The number of nodes in the repeater chain.
    - max_dists (int): The maximum number of rounds of distillation to be performed.
    - gamma (float): The simmetricity of the swap tree.
    - kappa (float): The total number of rounds of distillation to be performed among all segments.
    - zeta (float): The parameter for deriving the mean of the distribution from which to sample distillation rounds per segment.
    - tau (float): The parameter for deriving the std. dev. of the distribution from which to sample distillation rounds per segment.
    """
    # Get and order the swap space by symmetry score (ascending)
    swap_space = get_swap_space(nodes-1)
    swap_space = sorted(swap_space, key=lambda x: x[0])
    logging.debug(f"Most symmetric shapes: {swap_space[-1][1]}, {swap_space[-2][1]}, {swap_space[-3][1]}") 
                 
    # Sample the sequence of swaps
    selected_idx = round(gamma * (len(swap_space)-1))
    _, swap_tree, swap_sequence = swap_space[selected_idx]
    logging.debug(f"Selected index: {selected_idx+1}/{len(swap_space)} \
                 \nSelected swap sequence: {swap_sequence}")

    # Define the parameters for sampling the distillation rounds
    v = 2 * (nodes - 1) - 1 # number of segments
    mu = zeta * (v-1) # mean of the distribution (if low, distillations are happening at the leaves (beginning))
    sigma = tau * (v-1) # std. dev. of the distribution

    # Sample the values for the rounds distillations for segments, from a normal distribution N(mu, sigma)
    dists_tuple = sample_asymmetric_protocol_distillations(kappa, mu, sigma, v, beta=max_dists)
    logging.debug(f"Sampled distillation rounds: {dists_tuple}")
    dist_tree = assign_dists_to_tree(swap_tree, dists_tuple)
    protocol = dist_tree.get_sequence()

    checkAsymProtocol(protocol, nodes-1)
    logging.debug(f"Generated protocol: {protocol}")
    return protocol


def sample_symmetric_protocol_element(strategy, strategy_weight, protocol, idx, dists_target):
    """
    Returns 0 (swap) or 1 (distillation) based on the input parameters.

    Examples:
        Scenario 1: I miss 3 slots and I have 3 distillations missing
            Thus, I distill with p = 100% 
            (i.e., the first coin is depending on what is missing: 3 distillations / 3 slots)
    
        Scenario 2: I still have 3 slots and I have 1 distillation missing
            First of all, I throw a coin
            With 1/3 probability I distill
            While, the 2/3 remaining are distributed as it follows:
            - if I am dists_first, I distill with a prob. of strategy_weight
            - if I am swaps_first, I swap with a prob. of strategy_weight 
            - if I am alternate, 
                - if the previous was swap, I distill with a prob. of strategy_weight
                - if the previous was distill, I swap with a prob. of strategy_weight
    """
    dists_so_far = protocol.count(1)
    dists_remaining = dists_target - dists_so_far
    slots_remaining = len(protocol) - idx

    # Modeling the impact of the distillations applied so far on the decision
    if dists_remaining == 0:
        return 0
    
    dist_prob = dists_remaining/slots_remaining
    first_coin = np.random.choice([0, 1], p=[1 - dist_prob, dist_prob])
    if first_coin == 1:
        return 1
    else: # Modeling the impact of the strategies on the decision
        if strategy == "dists_first":
            return np.random.choice([0, 1], p=[1 - strategy_weight, strategy_weight])
        
        elif strategy == "swaps_first":
            return np.random.choice([0, 1], p=[strategy_weight, 1 - strategy_weight])
        
        elif strategy == "alternate":
            if idx == 0:
                return np.random.choice([0, 1])
            if protocol[idx-1] == 1:
                return np.random.choice([0, 1], p=[1 - strategy_weight, strategy_weight])
            if protocol[idx-1] == 0:
                return np.random.choice([0, 1], p=[strategy_weight, 1 - strategy_weight])

        else:
            raise ValueError("Invalid strategy")
        

def get_protocol_from_strategy(strategy, strategy_weight, number_of_swaps, number_of_dists):
    """
    Returns the protocol to be tested based on the input parameters.
    """
    protocol = [None] * (number_of_swaps + number_of_dists)

    for idx, _ in enumerate(protocol):
        protocol[idx] = int(sample_symmetric_protocol_element(strategy, strategy_weight, protocol, idx, number_of_dists))

    assert protocol.count(1) == number_of_dists, f"Expected {number_of_dists} distillations, got {protocol.count(1)}"
    return tuple(protocol)


def get_protocol_from_center_and_spacing(eta, tau, n, k):
    """
    Generates a protocol for distillation based on a normal distribution.
    The protocol is determined by sampling from the distribution and setting specific
        positions to 1, indicating where distillations should occur.

    Parameters:
    - eta: Center of mass, between -1 and 1.
    - tau: Spacing of the distribution, between 0 and 1.
    - n: Number of SWAPs.
    - k: Number of distillations.
    """
    l = n + k

    # Compute the mean and standard deviation of the normal distribution
    mu = (eta + 1) * (l + 1) / 2 
    sigma = l * tau
    dist = norm(loc=mu, scale=sigma)

    # Compute the PDF for a range of values from 0 to l
    pdf_values = dist.pdf(range(0, l))

    for i, pdf in enumerate(pdf_values):
        logging.debug(f"PDF at index {i}: {pdf}")

    # Sample from the normal distribution n times
    dist_idxs = []
    for i in range(k):
        uniform_sample = np.random.uniform(low=0, high=sum(pdf_values))
        idx = np.searchsorted(np.cumsum(pdf_values), uniform_sample)

        assert (idx >= 0 and idx < l), f"Sampled index {idx} is out of bounds."
        dist_idxs.append(int(idx))

        # Avoid sampling the same value again
        pdf_values[idx] = 0

    dist_idxs.sort()

    # Generate the protocol based on sampled indices
    protocol = [0] * l
    for idx in dist_idxs:
        protocol[idx] = 1

    logging.debug(f"Generated protocol: {tuple(protocol)}")

    assert protocol.count(0) == n, f"Expected {n} swapping, got {protocol.count(0)}"
    assert protocol.count(1) == k, f"Expected {k} distillations, got {protocol.count(1)}"
    return tuple(protocol)


def get_protocol_from_distillations(number_of_swaps, number_of_dists, where_to_distill=None):
    """
    Returns the protocol to be tested based on the input parameters.
    
    Parameters:
    - number_of_swaps (int): The number of swaps to be performed.
    - number_of_distillation (int): The number of distillation to be performed.
    - where_to_distill (int): The level at which to perform distillation.
    
    Returns:
    - tuple: the protocol to be tested.
    """
    distillations = [1] * number_of_dists
    swaps = [0] * number_of_swaps

    if number_of_dists == 0:
        protocol = swaps
    else:
        protocol = swaps[:where_to_distill] + distillations + swaps[where_to_distill:]
    
    return tuple(protocol)


def get_t_trunc(p_gen, p_swap, t_coh, nested_swaps, nested_dists, epsilon=0.01):
    """
    This function is derived from Brand et. al, with an adjustment for distillation.
    TODO: it is a very lossy bound, it should be improved, to get the simulation going faster (mantaining cdf coverage).
    Returns the truncation time based on a lower bound of what is sufficient to reach (1-epsilon) of the simulation cdf.
    """
    t_trunc = int((2/p_swap)**(nested_swaps) * (1/p_gen) * (1/epsilon)) # not considering distillation

    p_dist = 0.5 # in the worst case, p_dist will never go below 0.5
    t_trunc *= (1/p_dist)**(nested_dists) # considering distillation, but very unprecise

    # Introduce a factor to reduce the truncation time, as the previous bound is very lossy
    reduce_factor = 5
    t_trunc = min(max(t_coh, t_trunc//reduce_factor), t_coh * 300)
    return int(t_trunc)


def get_ordered_results(result: OptimizeResult, space_type: SpaceType, number_of_swaps) -> List[Tuple[np.float64, Tuple[int]]]:
    """
    This function adjust the results to be positive and returns an ordered list of (key_rate, protocol)
    """
    result.fun = -result.fun
    result.func_vals = [-val for val in result.func_vals]
    assert len(result.x_iters) == len(result.func_vals)
    
    if space_type == "one_level":
        result_tuples = [(  result.func_vals[i], 
                            get_protocol_from_distillations(
                                number_of_swaps=number_of_swaps,
                                number_of_dists=result.x_iters[i][0], 
                                where_to_distill=result.x_iters[i][1])
                         )  
                        for i in range(len(result.func_vals))]

    elif space_type == "enumerate":
        result_tuples = [(result.func_vals[i], ast.literal_eval(result.x_iters[i][0])) for i in range(len(result.func_vals))]

    elif space_type == "strategy" or space_type == "centerspace" or space_type == "asymmetric":
        result_tuples = [(result.func_vals[i], result.x_iters[i][-1]) for i in range(len(result.func_vals))]

    ordered_results = sorted(result_tuples, key=lambda x: x[0], reverse=True)
    return ordered_results


def write_results(filename, parameters, best_results):
    """
    Write optimization results to file.
    """
    with open(filename, 'a') as file:
        output = (
                    f"\nProtocol parameters:\n"
                    f"{{\n"
                    f"    't_coh': {parameters['t_coh']},\n"
                    f"    'p_gen': {parameters['p_gen']},\n"
                    f"    'p_swap': {parameters['p_swap']},\n"
                    f"    'w0': {parameters['w0']}\n"
                    f"}}\n\n")
            
        for number_of_swaps, (best_protocol, best_score, error_coverage) in best_results.items():
            if best_protocol is None:
                print(f"CDF under threshold for {number_of_swaps} swaps")
                output += (
                        f"CDF under threshold for {number_of_swaps} swaps:\n"
                        f"    CDF coverage: {error_coverage*100}%\n\n"
                    )
            else:
                output += (
                        f"Best configuration for {number_of_swaps} swaps:\n"
                        f"    Best Protocol: {best_protocol}\n"
                        f"    Best secret key rate: {best_score}\n\n"
                    )
            
        file.write(output)


def load_config(config_file):
    with open(config_file, 'r') as file:
        config = json.load(file)
    return config