"""
This script is used to run simulations to compare different quantum repeater protocols 
and visualize the performance of these protocols under various conditions. 
We are trying to compare strategies for distillation, which include distill-as-fast-as-possible and swap-as-fast-as-possible
"""

import matplotlib.pyplot as plt
colorblind_palette = [
    "#0072B2",
    "#E69F00",
    "#56B4E9",
    "#009E73",
    "#D55E00",
    "#CC79A7",
    "#F0E442",
    "#000000",
]
plt.rcParams['axes.prop_cycle'] = plt.cycler(color=colorblind_palette)

import copy
import numpy as np

from repeater_algorithm import RepeaterChainSimulation, repeater_sim, plot_algorithm
from repeater_mc import repeater_mc, plot_mc_simulation
from logging_utilities import (
    log_init, log_params, log_finish, create_iter_kwargs, save_data)
from utility_functions import secret_key_rate

from utility_functions import pmf_to_cdf
from matplotlib.ticker import MaxNLocator


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
    """
    new_w_func = w_func.copy()
    for t in range(len(pmf)):
        if pmf[t] < threshold:
            new_w_func[t] = np.nan
    return new_w_func


def save_plot(fig, axs, row_titles, parameters={}, rate=None, exp_name="protocol.png", legend=False, general_title=None):
    """
    Formats the input figure and axes.
    """
    if axs.ndim == 2:
        rows, cols = axs.shape
        for i in range(rows):
            for j in range(cols):
                axs[i, j].xaxis.set_major_locator(MaxNLocator(integer=True))
            if legend:
                axs[i, -1].legend()
    else:  # axs is 1D
        for ax in axs:
            ax.xaxis.set_major_locator(MaxNLocator(integer=True))
        if legend:
            axs[-1].legend()

    if general_title is not None:
        fig.suptitle(general_title)

    if row_titles is not None:
        for ax, row_title in zip(axs[:,-1], row_titles):
            ax.text(1.075, 0.5, row_title, transform=ax.transAxes, ha="left", va="center", rotation=90, fontsize=14)

    right_space = 0.95 if row_titles is not None else 0.975
    plt.tight_layout()
    plt.subplots_adjust(right=right_space, top=(0.75 + axs.shape[0]*0.075), hspace=0.2*axs.shape[0])

    fig.savefig(f"{exp_name}.png")
    

def plot_pmf_cdf_werner(pmf, w_func, trunc, axs, row, full_werner=True, label=None):
    """
    Plots (on the input axs) the PMF, CDF, and Werner parameter arrays (one row of subplots),
    making deep copies of the input arrays to ensure the originals are not modified.
    """
    assert len(pmf) >= trunc, "The length of pmf must be larger or equal to t_trunc"

    pmf_copy = copy.deepcopy(pmf)[:trunc]
    w_func_copy = copy.deepcopy(w_func)[:trunc]

    w_func_copy[0] = np.nan
    cdf = pmf_to_cdf(pmf_copy)
    
    plot_data = {
        "PMF": pmf_copy,
        "CDF": cdf,
        "Werner parameter": remove_unstable_werner(pmf_copy, w_func_copy)
    }
    
    plot_axs = axs[row] if axs.ndim == 2 else axs  # handle both 1D and 2D axes arrays
    
    for title, data in plot_data.items():
        ax = plot_axs[list(plot_data.keys()).index(title)]
        if label is not None and title == "Werner parameter":
            ax.plot(np.arange(trunc), data, label=label)
        else:
            ax.plot(np.arange(trunc), data)
        ax.set_xlabel("Waiting Time")
        ax.set_title(title)
        if title == "Werner parameter":
            if full_werner:
                ax.set_ylim([0, 1])
            ax.set_ylabel("Werner parameter")            
        else:
            ax.set_ylabel("Probability")


def get_protocol_rate(parameters):
    """
    Returns the secret key rate for the input parameters.
    """
    pmf, w_func = repeater_sim(parameters)
    return secret_key_rate(pmf, w_func, parameters["t_trunc"])

def get_protocol(number_of_swaps, number_of_dists, where_to_distill=None):
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

def sim_distillation_strategies(parameters_set = [{"p_gen": 0.5, "p_swap": 0.5, "t_trunc": 100000, 
                                    "t_coh": 400, "w0": 0.9}]):
    """
        For a set of parameters we test different protocols to compare distillation strategies.
        On the x-axis we have the step at which the distillation is applied.
        On the y-axis we have the rate for the protocol.
        A costant line is added to benchmark the protocol in the case where no distillation is applied.
    """
    SWAPS = range(1, 4)
    DISTS = range(1, 6, 2)
    fig, axs = plt.subplots(len(parameters_set), len(SWAPS), figsize=(15, 4*len(parameters_set)))

    for i, parameters in enumerate(parameters_set):
        print(f"\nParameters: {parameters}")

        for j, number_of_swaps in enumerate(SWAPS):
            print(f"\n{number_of_swaps}-level(s) of swapping...")
        
            protocol = get_protocol(number_of_swaps=number_of_swaps, number_of_dists=0)
            parameters["protocol"] = protocol
            print(f"Protocol {protocol},\t r = {get_protocol_rate(parameters)}") # DEBUG
            benchmark = get_protocol_rate(parameters)

            if axs.ndim == 1:
                ax = axs[j]
            else:
                ax = axs[i, j]

            for number_of_dists in DISTS:
                plot_results = []
                print() # DEBUG 

                for where_to_distill in range(0, number_of_swaps+1):
                    protocol = get_protocol(number_of_swaps=number_of_swaps, number_of_dists=number_of_dists, 
                                                where_to_distill=where_to_distill)
                    parameters["protocol"] = protocol

                    plot_results.append(get_protocol_rate(parameters))
                    print(f"Protocol {protocol},\t r = {get_protocol_rate(parameters)}") # DEBUG
            
                ax.plot(np.arange(number_of_swaps+1), np.array(plot_results), 
                            label=f"{number_of_dists} {'distillations' if number_of_dists > 1 else 'distillation'}")

            ax.axhline(y=benchmark, color='r', linestyle='--', label="No distillation")
            ax.set_xlabel("Nesting level at which distillation is applied")
            ax.set_ylabel("Secret Key Rate")
            ax.set_title(f"{number_of_swaps} {'swaps' if number_of_swaps > 1 else 'swap'}")
            ax.legend()

    title_params = f"p_gen = {parameters['p_gen']}, p_swap = {parameters['p_swap']}, t_coh = {parameters['t_coh']}"
    row_titles = [f"({index_lowercase_alphabet(idx)}) w0 = {parameters['w0']}" for idx, parameters in enumerate(parameters_set)] if len(parameters_set) > 1 else None
    save_plot(fig=fig, axs=axs, row_titles=row_titles, parameters=parameters, 
              rate=None, exp_name="distillation_strategies", general_title=title_params)
    
if __name__ == "__main__":
    parameters_set = [
        {"p_gen": 0.5, "p_swap": 0.5, "t_trunc": 50000, "t_coh": 600, "w0": 0.9},
        {"p_gen": 0.5, "p_swap": 0.5, "t_trunc": 50000, "t_coh": 600, "w0": 0.99999}     
        ]
    
    sim_distillation_strategies(parameters_set)