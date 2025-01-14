import time
import warnings
from copy import deepcopy
from collections.abc import Iterable
import logging

import matplotlib.pyplot as plt
import numba as nb
import numpy as np

from repeater_types import checkAsymProtocol
try:
    import cupy as cp # type: ignore
    _cupy_exist = True
except (ImportError, ModuleNotFoundError):
    _cupy_exist = False

from protocol_units import join_links_compatible
from protocol_units_efficient import join_links_efficient
from utility_functions import secret_key_rate, ceil, werner_to_fid, find_heading_zeros_num, matrix_to_werner, werner_to_matrix, get_fidelity
from logging_utilities import log_init, create_iter_kwargs, save_data
from repeater_mc import repeater_mc, plot_mc_simulation


__all__ = ["RepeaterChainSimulation", "compute_unit", "plot_algorithm",
           "join_links_compatible", "repeater_sim"]


class HashableParameters():
    def __init__(self, parameters):
        self.parameters = parameters
    
    def __hash__(self):
        return hash(frozenset(self.parameters.items()))
    
    def __eq__(self, other):
        if isinstance(other, HashableParameters):
            return frozenset(self.parameters.items()) == frozenset(other.parameters.items())
        return False
    
    def set(self, key, value):
        self.parameters[key] = value

    def __repr__(self):
        return f"HashableParameters{self.parameters['protocol'] or '()'}"
    

class RepeaterChainSimulation():
    def __init__(self, use_cache=False):
        self.use_cache = use_cache
        self.use_fft = True
        self.use_gpu = False
        self.gpu_threshold = 1000000
        self.efficient = True
        self.zero_padding_size = None
        self._qutip = False
        if self.use_cache:
            self.cache = {} # parameters: (pmf, w_func) -- or -- parameters: full_result

    def iterative_convolution(self,
            func, shift=0, first_func=None, p_swap=None):
        """
        Calculate the convolution iteratively:
        first_func * func * func * ... * func
        It returns the sum of all iterative convolution:
        first_func + first_func * func + first_func * func * func ...

        Parameters
        ----------
        func: array-like
            The function to be convolved in array form.
            It is always a probability distribution.
        shift: int, optional
            For each k the function will be shifted to the right. Using for
            time-out mt_cut.
        first_func: array-like, optional
            The first_function in the convolution. If not given, use func.
            It can be different because the first_func is
            `P_s` and the `func` P_f.
            It is upper bounded by 1.
            It can be a probability, or an array of states.
        p_swap: float, optimal
            Entanglement swap success probability.

        Returns
        -------
        sum_convolved: array-like
            The result of the sum of all convolutions.
        """
        if first_func is None or len(first_func.shape) == 1:
            is_dm = False
        else:
            is_dm = True

        trunc = len(func)

        # determine the required number of convolution
        if shift != 0:
            # cut-off is added here.
            # because it is a constant, we only need size/mt_cut convolution.
            max_k = int(np.ceil((trunc/shift)))
        else:
            max_k = trunc
        if p_swap is not None:
            pf = np.sum(func) * (1 - p_swap)
        else:
            pf = np.sum(func)
        with np.errstate(divide='ignore'):
            if pf <= 0.:  # pf ~ 0 and round-off error
                max_k = trunc
            else:
                max_k = min(max_k, (-52 - np.log(trunc))/ np.log(pf))
        if max_k > trunc:
            print(max_k)
            print(trunc)
        max_k = int(max_k)

        # Transpose the array of state to the shape (1,1,trunc)
        # if werner or shape (4,4,trunc) if density matrix
        if first_func is None:
            first_func = func
        if not is_dm:
            first_func = first_func.reshape((trunc, 1, 1))
        first_func = np.transpose(first_func, (1, 2, 0))

        # Convolution
        result = np.empty(first_func.shape, first_func.dtype)
        for i in range(first_func.shape[0]):
            for j in range(first_func.shape[1]):
                result[i][j] = self.iterative_convolution_helper(
                    func, first_func[i][j], trunc, shift, p_swap, max_k)

        # Permute the indices back
        result = np.transpose(result, (2, 0, 1))
        if not is_dm:
            result = result.reshape(trunc)

        return result

    def iterative_convolution_helper(
            self, func, first_func, trunc, shift, p_swap, max_k):
        # initialize the result array
        sum_convolved = np.zeros(trunc, dtype=first_func.dtype)
        if p_swap is not None:
            sum_convolved[:len(first_func)] = p_swap * first_func
        else:
            sum_convolved[:len(first_func)] = first_func

        if shift <= trunc:
            zero_state = np.zeros(shift, dtype=func.dtype)
            func = np.concatenate([zero_state, func])[:trunc]

        # decide what convolution to use and prepare the data
        convolved = first_func
        if self.use_fft: # Use geometric sum in Fourier space
            shape = 2 * trunc - 1
            # The following is from SciPy, they choose the size to be 2^n,
            # It increases the accuracy.
            if self.zero_padding_size is not None:
                shape = self.zero_padding_size
            else:
                shape = 2 ** np.ceil(np.log2(shape)).astype(int)
            if self.use_gpu and not _cupy_exist:
                logging.warning("CuPy not found, using CPU.")
                self.use_gpu = False
            if self.use_gpu and shape > self.gpu_threshold:
                # transfer the data to GPU
                sum_convolved = cp.asarray(sum_convolved)
                convolved = cp.asarray(convolved)
                func = cp.asarray(func)
            if self.use_gpu and shape > self.gpu_threshold:
                # use CuPy fft
                ifft = cp.fft.ifft
                fft = cp.fft.fft
                to_real = cp.real
            else:
                # use NumPy fft
                ifft = np.fft.ifft
                fft = np.fft.fft
                to_real = np.real

            convolved_fourier = fft(convolved, shape)
            func_fourier = fft(func, shape)

            if p_swap is not None:
                result= ifft(
                    p_swap*convolved_fourier / (1 - (1-p_swap) * func_fourier))
            else:
                result= ifft(convolved_fourier / (1 - func_fourier))

            # validity check
            last_term = abs(result[-1])
            if last_term > 10e-16:
                logging.warning(
                    f"The size of zero-padded array, shape={shape}, "
                    "for the Fourier transform is not big enough. "
                    "The resulting circular convolution might contaminate "
                    "the distribution."
                    f"The deviation is as least {float(last_term):.0e}.")

            result = to_real(result[:trunc])
            if self.use_gpu and shape > self.gpu_threshold:
                result = cp.asnumpy(result)

        else:  # Use exact convolution
            zero_state = np.zeros(trunc - len(convolved), dtype=convolved.dtype)
            convolved = np.concatenate([convolved, zero_state])
            for k in range(1, max_k):
                convolved = np.convolve(convolved[:trunc], func[:trunc])
                if p_swap is not None:
                    coeff = p_swap*(1-p_swap)**(k)
                    sum_convolved += coeff * convolved[:trunc]
                else:
                    sum_convolved += convolved[:trunc]
            result = sum_convolved
        return result

    def entanglement_swap(self,
            pmf1, w_func1, pmf2, w_func2, p_swap,
            cutoff, t_coh, cut_type):
        """
        Calculate the waiting time and average Werner parameter with time-out
        for entanglement swap.

        Parameters
        ----------
        pmf1, pmf2: array-like 1-D
            The waiting time distribution of the two input links.
        w_func1, w_func2: array-like 1-D
            The Werner parameter as function of T of the two input links.
        p_swap: float
            The success probability of entanglement swap.
        cutoff: int or float
            The memory time cut-off, werner parameter cut-off, or
            run time cut-off.
        t_coh: int
            The coherence time.
        cut_type: str
            `memory_time`, `fidelity` or `run_time`.

        Returns
        -------
        t_pmf: array-like 1-D
            The waiting time distribution of the entanglement swap.
        w_func: array-like 1-D
            The Werner parameter as function of T of the entanglement swap.
        """
        if self.efficient and cut_type == "memory_time":
            join_links = join_links_efficient
            if self._qutip:
                # only used for testing, very slow
                # join_links_state = join_links_matrix_qutip
                pass
            else:
                join_links_state = join_links_efficient
        else:
            join_links = join_links_compatible
            join_links_state = join_links_compatible
        if cut_type == "memory_time":
            shift = cutoff
        else:
            shift = 0

        # P'_f
        pf_cutoff = join_links(
            pmf1, pmf2, w_func1, w_func2, ycut=False,
            cutoff=cutoff, cut_type=cut_type, evaluate_func="1", t_coh=t_coh)
        # P'_s
        ps_cutoff = join_links(
            pmf1, pmf2, w_func1, w_func2, ycut=True,
            cutoff=cutoff, cut_type=cut_type, evaluate_func="1", t_coh=t_coh)
        # P_f or P_s (Differs only by a constant p_swap)
        pmf_cutoff = self.iterative_convolution(
            pf_cutoff, shift=shift,
            first_func=ps_cutoff)
        del ps_cutoff
        # Pr(Tout = t)
        pmf_swap = self.iterative_convolution(
            pmf_cutoff, shift=0, p_swap=p_swap)

        # Wsuc * P_s
        state_suc = join_links_state(
            pmf1, pmf2, w_func1=w_func1, w_func2=w_func2, ycut=True,
            cutoff=cutoff, cut_type=cut_type,
            t_coh=t_coh, evaluate_func="w1w2")
        # Wprep * Pr(Tout = t)
        state_prep = self.iterative_convolution(
            pf_cutoff,
            shift=shift, first_func=state_suc)
        del pf_cutoff, state_suc
        # Wout * Pr(Tout = t)
        state_out = self.iterative_convolution(
            pmf_cutoff, shift=0,
            first_func=state_prep, p_swap=p_swap)
        del pmf_cutoff

        with np.errstate(divide='ignore', invalid='ignore'):
            if len(state_out.shape) == 1:
                state_out[1:] /= pmf_swap[1:]  # 0-th element has 0 pmf
                state_out = np.where(np.isnan(state_out), 1., state_out)
            else:
                state_out = np.transpose(state_out, (1, 2, 0))
                state_out[:,:,1:] /= pmf_swap[1:]  # 0-th element has 0 pmf
                state_out = np.transpose(state_out, (2, 1, 0))

        return pmf_swap, state_out


    def destillation(self,
            pmf1, w_func1, pmf2, w_func2,
            cutoff, t_coh, cut_type):
        """
        Calculate the waiting time and average Werner parameter
        with time-out for the distillation.

        Parameters
        ----------
        pmf1, pmf2: array-like 1-D
            The waiting time distribution of the two input links.
        w_func1, w_func2: array-like 1-D
            The Werner parameter as function of T of the two input links.
        cutoff: int or float
            The memory time cut-off, werner parameter cut-off, or 
            run time cut-off.
        t_coh: int
            The coherence time.
        cut_type: str
            `memory_time`, `fidelity` or `run_time`.

        Returns
        -------
        t_pmf: array-like 1-D
            The waiting time distribution of the distillation.
        w_func: array-like 1-D
            The Werner parameter as function of T of the distillation.
        """
        if self.efficient and cut_type == "memory_time":
            join_links = join_links_efficient
        else:
            join_links = join_links_compatible
        if cut_type == "memory_time":
            shift = cutoff
        else:
            shift = 0
        # P'_f  cutoff attempt when cutoff fails
        pf_cutoff = join_links(
            pmf1, pmf2, w_func1, w_func2, ycut=False,
            cutoff=cutoff, cut_type=cut_type,
            evaluate_func="1", t_coh=t_coh)
        # P'_ss  cutoff attempt when cutoff and dist succeed
        pss_cutoff = join_links(
            pmf1, pmf2, w_func1, w_func2, ycut=True,
            cutoff=cutoff, cut_type=cut_type,
            evaluate_func="0.5+0.5w1w2", t_coh=t_coh)
        # P_s  dist attempt when dist succeeds
        ps_dist = self.iterative_convolution(
            pf_cutoff, shift=shift,
            first_func=pss_cutoff)
        del pss_cutoff
        # P'_sf  cutoff attempt when cutoff succeeds but dist fails
        psf_cutoff = join_links(
            pmf1, pmf2, w_func1, w_func2, ycut=True,
            cutoff=cutoff, cut_type=cut_type,
            evaluate_func="0.5-0.5w1w2", t_coh=t_coh)
        # P_f  dist attempt when dist fails
        pf_dist = self.iterative_convolution(
            pf_cutoff, shift=shift,
            first_func=psf_cutoff)
        del psf_cutoff
        # Pr(Tout = t)
        pmf_dist = self.iterative_convolution(
            pf_dist, shift=0,
            first_func=ps_dist)
        del ps_dist

        # Wsuc * P'_ss
        state_suc = join_links(
            pmf1, pmf2, w_func1, w_func2, ycut=True,
            cutoff=cutoff, cut_type=cut_type,
            evaluate_func="w1+w2+4w1w2", t_coh=t_coh)
        # Wprep * P_s
        state_prep = self.iterative_convolution(
            pf_cutoff, shift=shift,
            first_func=state_suc)
        del pf_cutoff, state_suc
        # Wout * Pr(Tout = t)
        state_out = self.iterative_convolution(
            pf_dist, shift=0,
            first_func=state_prep)
        del pf_dist, state_prep

        with np.errstate(divide='ignore', invalid='ignore'):
            state_out[1:] /= pmf_dist[1:]
            state_out = np.where(np.isnan(state_out), 1., state_out)
        return pmf_dist, state_out


    def compute_unit(self,
            parameters, pmf1, w_func1, pmf2=None, w_func2=None,
            unit_kind="swap", step_size=1):
        """
        Calculate the the waiting time distribution and
        the Werner parameter of a protocol unit swap or distillation.
        Cut-off is built in swap or distillation.

        Parameters
        ----------
        parameters: dict
            A dictionary contains the parameters of
            the repeater and the simulation.
        pmf1, pmf2: array-like 1-D
            The waiting time distribution of the two input links.
        w_func1, w_func2: array-like 1-D
            The Werner parameter as function of T of the two input links.
        unit_kind: str
            "swap" or "dist"

        Returns
        -------
        t_pmf, w_func: array-like 1-D
            The output waiting time and Werner parameters
        """
        if pmf2 is None:
            pmf2 = pmf1
        if w_func2 is None:
            w_func2 = w_func1
        p_gen = parameters["p_gen"]
        p_swap = parameters["p_swap"]
        w0 = parameters["w0"]
        t_coh = parameters.get("t_coh", np.inf)
        cut_type = parameters.get("cut_type", "memory_time")
        if "cutoff" in parameters.keys():
            cutoff = parameters["cutoff"]
        elif cut_type == "memory_time":
            cutoff = parameters.get("mt_cut", np.iinfo(int).max)
        elif cut_type == "fidelity":
            cutoff = parameters.get("w_cut", 1.0e-16)  # shouldn't be zero
            if cutoff == 0.:
                cutoff = 1.0e-16
        elif cut_type == "run_time":
            cutoff = parameters.get("rt_cut", np.iinfo(int).max)
        else:
            cutoff = np.iinfo(int).max

        # type check (allow for list of p_gen)
        if isinstance(p_gen, Iterable):
            if not all(np.isreal(p) for p in p_gen):
                raise TypeError("p_gen must be a float number.")
        elif not np.isreal(p_gen):
            raise TypeError("p_gen must be a float number.")
        if isinstance(t_coh, Iterable):
            if not all(np.isreal(t) for t in t_coh):
                raise TypeError("The coherence time must be a real number.")
        elif not np.isreal(t_coh):
            raise TypeError(
                f"The coherence time must be a real number, not{t_coh}")
        if not np.isreal(p_swap):
            raise TypeError("p_swap must be a float number.")
        if cut_type in ("memory_time", "run_time") and not np.issubdtype(type(cutoff), np.integer):
            raise TypeError(f"Time cut-off must be an integer. not {cutoff}")
        if cut_type == "fidelity" and not (cutoff >= 0. or cutoff < 1.):
            raise TypeError(f"Fidelity cut-off must be a real number between 0 and 1.")
        # if not np.isreal(w0) or w0 < 0. or w0 > 1.:
        #     raise TypeError(f"Invalid Werner parameter w0 = {w0}")

        # swap or distillation for next level
        if unit_kind == "swap":
            pmf, w_func = self.entanglement_swap(
                pmf1, w_func1, pmf2, w_func2, p_swap,
                cutoff=cutoff, t_coh=t_coh, cut_type=cut_type)
        elif unit_kind == "dist":
            pmf, w_func = self.destillation(
                pmf1, w_func1, pmf2, w_func2,
                cutoff=cutoff, t_coh=t_coh, cut_type=cut_type)

        # erase ridiculous Werner parameters,
        # it can happen when the probability is too small ~1.0e-20.
        w_func = np.where(np.isnan(w_func), 1., w_func)
        w_func[w_func > 1.0] = 1.0
        w_func[w_func < 0.] = 0.

        # check probability coverage
        coverage = np.sum(pmf)
        if coverage < 0.99:
            logging.warning(
                "The truncation time only covers {:.2f}% of the distribution, "
                "please increase t_trunc.\n".format(
                    coverage*100))
        
        return pmf, w_func


    def check_cache(self, parameters, all_level=False):
        parameters_new = deepcopy(parameters)
        protocol_new = parameters_new.pop("protocol")
        trunc_new = parameters_new.pop("t_trunc")

        parameters_new_hash = HashableParameters(parameters_new)
        cached_result = None

        for i in range(len(protocol_new), 0, -1):
            # check if there is a cache for the protocol under investigation
            parameters_new_hash.set("protocol", protocol_new[:i])
            if parameters_new_hash in self.cache:
                if all_level:
                    trunc_cached = len(self.cache[parameters_new_hash][-1][0]) # TODO: check if this is correct
                else:
                    trunc_cached = len((self.cache[parameters_new_hash][0]))
                # check also if the cached results fully cover the truncation
                if trunc_cached >= trunc_new:
                    # start computing from i level with self.cache[parameters_hash][-1]
                    cached_result = self.cache[parameters_new_hash]
                    break
        else:
            # if no cache found, start from the beginning the computation
            i = 0
        
        return i, parameters_new_hash, cached_result
    
    def nested_protocol(self, parameters, all_level=False):
        """
        Compute the waiting time and the Werner parameter of a symmetric
        repeater protocol.

        Parameters
        ----------
        parameters: dict
            A dictionary contains the parameters of
            the repeater and the simulation.
        all_level: bool
            If true, Return a list of the result of all the levels.
            [(t_pmf0, w_func0), (t_pmf1, w_func1) ...]

        Returns
        -------
        t_pmf, w_func: array-like 1-D
            The output waiting time and Werner parameters
        """
        # check if there is a result cached for the parameters and one sub-protocol, starting from the longest
        i = 0
        if self.use_cache:
            i, parameters_hash, cached_result = self.check_cache(parameters, all_level)
            if cached_result is not None:
                logging.info(f"Using cached result: {parameters_hash.parameters['protocol']}")
        
        parameters = deepcopy(parameters)
        protocol = parameters["protocol"]
        # in case of 1-level protocol:
        # ensure protocol is treated as a tuple
        if isinstance(protocol, int): 
            protocol = (protocol,)
        p_gen = parameters["p_gen"]
        w0 = parameters["w0"]
        if "tau" in parameters:  # backward compatibility
            parameters["mt_cut"] = parameters.pop("tau")
        if "cutoff_dict" in parameters.keys():
            cutoff_dict = parameters["cutoff_dict"]
            mt_cut = cutoff_dict.get("memory_time", np.iinfo(int).max)
            w_cut = cutoff_dict.get("fidelity", 1.e-8)
            rt_cut = cutoff_dict.get("run_time", np.iinfo(int).max)
        else:
            mt_cut = parameters.get("mt_cut", np.iinfo(int).max)
            w_cut = parameters.get("w_cut", 1.e-8)
            rt_cut = parameters.get("rt_cut", np.iinfo(int).max)
        if "cutoff" in parameters:
            cutoff = parameters["cutoff"]
        if not isinstance(mt_cut, Iterable):
            mt_cut = (mt_cut,) * len(protocol)
        else:
            mt_cut = tuple(mt_cut)
        if not isinstance(w_cut, Iterable):
            w_cut = (w_cut,) * len(protocol)
        else:
            w_cut = tuple(w_cut)
        if not isinstance(rt_cut, Iterable):
            rt_cut = (rt_cut,) * len(protocol)
        else:
            rt_cut = tuple(rt_cut)

        t_trunc = parameters["t_trunc"]

        # if some intermediate result exists, starts computing from i level with it
        if i > 0:
            if all_level:
                full_result = cached_result
            else:
                pmf, w_func = cached_result
        else:
            # elementary link
            t_list = np.arange(1, t_trunc)
            pmf = p_gen * (1 - p_gen)**(t_list - 1)
            pmf = np.concatenate((np.array([0.]), pmf))
            w_func = np.array([w0] * t_trunc)
            if all_level:
                full_result = [(pmf, w_func)]
            
        total_step_size = 1

        # Compute protocol units, eventually caching partial results
        while i < len(protocol):
            # Prepare the hash for the caching
            if self.use_cache:
                curr_protocol = protocol[:i+1]
                parameters_hash = deepcopy(parameters_hash)
                parameters_hash.set("protocol", curr_protocol)

            operation = protocol[i]
            if "cutoff" in parameters and isinstance(cutoff, Iterable):
                parameters["cutoff"] = cutoff[i]
            parameters["mt_cut"] = mt_cut[i]
            parameters["w_cut"] = w_cut[i]
            parameters["rt_cut"] = rt_cut[i]

            if operation == 0:
                pmf, w_func = self.compute_unit(
                    parameters, pmf, w_func, unit_kind="swap", step_size=total_step_size)
            elif operation == 1:
                pmf, w_func = self.compute_unit(
                    parameters, pmf, w_func, unit_kind="dist", step_size=total_step_size)
            
            if all_level:
                full_result.append((pmf, w_func))
                if self.use_cache:
                    self.cache[parameters_hash] = full_result
            else:
                if self.use_cache:
                    self.cache[parameters_hash] = (pmf, w_func)
            i += 1

        final_pmf = pmf
        final_w_func = w_func
        if all_level:
            return full_result
        else:
            return final_pmf, final_w_func


    def find_right_segment(self, segments, start_index):
        r_segm = start_index + 1
        while r_segm < len(segments) and segments[r_segm] is None:
            r_segm += 1
        if r_segm >= len(segments):
            raise ValueError("No non-None segment found after index {}".format(start_index))
        return r_segm


    def validate_heterogeneous_parameters(self, parameters, number_of_segments):
        """
        Validate the parameters of a heterogeneous protocol.
        """
        if not isinstance(parameters["w0"], Iterable) or not isinstance(parameters["t_coh"], Iterable):
            raise ValueError("w0 and t_coh must be iterable.")
        if len(parameters["w0"]) != number_of_segments or len(parameters["p_gen"]) != number_of_segments:
            raise ValueError("The number of segments must match the number of p_gen and w0 values.")
        if len(parameters["t_coh"]) != number_of_segments + 1:
            raise ValueError("The number of nodes must match the number of t_coh values.")


    def asymmetric_protocol(self, parameters, number_of_segments):
        """
        Compute the waiting time and the Werner parameter of an asymmetric protocol.
        Parameters
        ----------
        parameters: dict
            A dictionary contains the parameters of
            the repeater and the simulation.

        Cut-offs and 'all_level' are not implemented.

        Returns
        -------
        t_pmf, w_func: array-like 1-D
            The output waiting time and Werner parameters
        """
        # Check if it is a heterogeneous protocol
        # If yes, heterogeneous doesn't support cut-offs and 'all_level' yet
        if isinstance(parameters["p_gen"], Iterable):
            self.validate_heterogeneous_parameters(parameters, number_of_segments)
            if "cutoff" in parameters:
                raise NotImplementedError("Cut-offs are not implemented for heterogeneous protocols.")
            if "all_level" in parameters:
                raise NotImplementedError("All levels are not implemented for heterogeneous protocols.")
            return self.asymmetric_heterogeneous_protocol(parameters, number_of_segments)
        
        S = number_of_segments
        parameters = deepcopy(parameters)

        protocol = parameters["protocol"]
        p_gen = parameters["p_gen"]
        w0 = parameters["w0"]
        t_trunc = parameters["t_trunc"]
        t_list = np.arange(1, t_trunc)

        # In case of 1-level protocol, ensure protocol is treated as a tuple
        if isinstance(protocol, str): 
            protocol = (protocol,)

        # Each segment will keep a distribution for waiting time and Werner parameter
        segments = []

        # Elementary link: for each segment, generate its distribution
        for _ in range(S):
            pmf = p_gen * (1 - p_gen)**(t_list - 1)
            pmf = np.concatenate((np.array([0.]), pmf))
            w_func = np.array([w0] * t_trunc)
            segments.append((pmf, w_func))

        # Compute step by step the whole protocol
        # Given idx as the index of the segment (or left segment in case of swap)
        for i in range(len(protocol)):
            step: str = protocol[i]
            operation = step[0]
            idx = int(step[1:]) 
            curr_segment = segments[idx] 
            
            if operation == 's':
                next_idx = self.find_right_segment(segments, idx)
                next_segment = segments[next_idx]
                pmf, w_func = self.compute_unit(
                    parameters, *curr_segment, *next_segment, unit_kind="swap", step_size=1)
                segments[idx] = None
                segments[next_idx] = (pmf, w_func)

            elif operation == 'd':
                pmf, w_func = self.compute_unit(
                    parameters, *curr_segment, unit_kind="dist", step_size=1)
                segments[idx] = (pmf, w_func)

        return next((segm for segm in segments if segm is not None), (None, None))


    def asymmetric_heterogeneous_protocol(self, parameters, number_of_segments):
        """
        Compute the waiting time and the Werner parameter of an asymmetric protocol.
        """
        # Use join_links_compatible instead of join_links_efficient, as coherence time is not homogeneous
        # self.efficient = False
        
        S = number_of_segments
        parameters = deepcopy(parameters)

        protocol = parameters["protocol"]
        p_gens = parameters["p_gen"]
        w0s = parameters["w0"]
        t_trunc = parameters["t_trunc"]
        t_list = np.arange(1, t_trunc)
        t_cohs = parameters["t_coh"]

        # In case of 1-level protocol, ensure protocol is treated as a tuple
        if isinstance(protocol, str):
            protocol = (protocol,)

        # Each segment will keep
        # - an integer for the segment length
        # - a distribution for waiting time and Werner parameter
        segments = []

        # Elementary link: for each segment, generate its distribution
        for i in range(S):
            pmf = p_gens[i] * (1 - p_gens[i])**(t_list - 1)
            pmf = np.concatenate((np.array([0.]), pmf))
            w_func = np.array([w0s[i]] * t_trunc)
            # Keep track of segment endpoints
            segments.append((pmf, w_func, i, i+1))

        # Compute step by step the whole protocol
        # Given idx as the index of the segment (or left segment in case of swap)
        for i in range(len(protocol)):
            step: str = protocol[i]
            operation = step[0]
            idx = int(step[1:])
            curr_segment = segments[idx]

            if operation == 's':
                next_idx = self.find_right_segment(segments, idx)
                next_segment = segments[next_idx]
                assert curr_segment[3] == next_segment[2], f"Segments {curr_segment[2]} and {next_segment[3]} are not compatible."
                parameters["t_coh"] = [t_cohs[curr_segment[2]], t_cohs[next_segment[2]], t_cohs[next_segment[3]]] 
                pmf, w_func = self.compute_unit(
                    parameters, curr_segment[0], curr_segment[1], next_segment[0], next_segment[1], 
                    unit_kind="swap", step_size=1)
                segments[idx] = None
                segments[next_idx] = (pmf, w_func, curr_segment[2], next_segment[3])
            
            elif operation == 'd':
                parameters["t_coh"] = [t_cohs[curr_segment[2]], t_cohs[curr_segment[3]]]
                pmf, w_func = self.compute_unit(
                    parameters, curr_segment[0], curr_segment[1], unit_kind="dist", step_size=1)
                segments[idx] = (pmf, w_func, curr_segment[2], curr_segment[3])

        final_segment = next((segm for segm in segments if segm is not None), (None, None))
        return (final_segment[0], final_segment[1])


def compute_unit(
        parameters, pmf1, w_func1, pmf2=None, w_func2=None,
        unit_kind="swap", step_size=1):
    """
    Functional warpper for compute_unit
    """
    simulator = RepeaterChainSimulation()
    return simulator.compute_unit(
        parameters=parameters, pmf1=pmf1, w_func1=w_func1, pmf2=pmf2, w_func2=w_func2, unit_kind=unit_kind, step_size=step_size)


def repeater_sim(parameters, all_level=False):
    """
    Functional wrapper for nested_protocol
    A first typecheck on the protocol is done to identify the simulation to run, i.e.
    If the protocol is a tuple of integers, run the nested protocol
    Otherwise, the tuples should be of strings, so run the asymmetric protocol
    """
    simulator = RepeaterChainSimulation()

    if isinstance(parameters["protocol"], Iterable) and all(isinstance(i, int) for i in parameters["protocol"]):
        return simulator.nested_protocol(parameters=parameters, all_level=all_level)
    elif isinstance(parameters["protocol"], Iterable) and all(isinstance(i, str) for i in parameters["protocol"]):
        number_of_segments = checkAsymProtocol(parameters["protocol"])
        return simulator.asymmetric_protocol(parameters, number_of_segments)
    else:
        raise ValueError("The protocol must be a tuple of integers or strings.")


def plot_algorithm(pmf, fid_func, axs=None, t_trunc=None, legend=None):
    """
    Plot the waiting time distribution and Werner parameters
    """
    cdf = np.cumsum(pmf)
    if t_trunc is None:
        try:
            t_trunc = np.min(np.where(cdf >= 0.997))
        except ValueError:
            t_trunc = len(pmf)
    pmf = pmf[:t_trunc]
    fid_func = fid_func[:t_trunc]
    fid_func[0] = np.nan

    axs[0][0].plot((np.arange(t_trunc)), np.cumsum(pmf))

    axs[0][1].plot((np.arange(t_trunc)), pmf)
    axs[0][1].set_xlabel("Waiting time $T$")
    axs[0][1].set_ylabel("Probability")

    axs[1][0].plot((np.arange(t_trunc)), fid_func)
    axs[1][0].set_xlabel("Waiting time $T$")
    axs[1][0].set_ylabel("Werner parameter")

    axs[0][0].set_title("CDF")
    axs[0][1].set_title("PMF")
    axs[1][0].set_title("Werner")
    if legend is not None:
        for i in range(2):
            for j in range(2):
                axs[i][j].legend(legend)
    plt.tight_layout()