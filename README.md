# repeater-cut-off-optimization
This repository stores the implementation of the algorithm introduced in *Efficient optimization of cut-offs in quantum repeater chains* by Boxi Li, Tim Coopmans and David Elkouss. It includes two implementations: 
- The numerical algorithm calculating the waiting time distribution and the fidelity of the delivered entangled state.
- The optimizer used to optimize the cut-off time for maximal secret key rate.

## Download
To download or clone the repository, using the green button `Clone or download`.

## Prerequisites
The following Python packages are required for running the core algorithms:
```
NumPy, Scipy, Numba
```
In addition, we use `Matplotlib` for plotting and `pytest` for unit tests.

For GPU accelerated convolution, you will need
```
CuPy
```
See [CuPy installation](https://docs-cupy.chainer.org/en/stable/install.html) for details

## File overview
- The protocol units such as entanglement swap, distillation or cut-off are defined in `protocol_units.py`.
- The core code for the numerical simulation of repeater chains is under `repeater_algorithm.py`.
- The optimizer can be found in `optimize_cutoff.py`.
- Examples for some symmetric repeater protocol are given in `examples.py`

## License
This project is licensed under the MIT License - see the [LICENSE.md](LICENSE.md) file for details.
