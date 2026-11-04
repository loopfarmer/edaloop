"""A quasi-static reference for a coplanar line: a 2D field solve of the cross-section, with no EM tool in it.

Solvers can agree or disagree for reasons of their own meshes. This is a different method on the same drawing: Laplace's
equation by finite volumes on a graded grid, with perfect conductors. Z0 comes from the capacitance with and without the
dielectric. It ignores conductor loss and dispersion, so it's a sanity reference for a low-loss line, not a measurement.
"""
from __future__ import annotations

import math

import numpy as np
import scipy.sparse as sparse
import scipy.sparse.linalg as sparse_linalg

C0 = 299792458.0
E0 = 8.8541878128e-12


def _graded(points: list[float], cell: float, ratio: float = 1.2, largest: float = 150.0) -> np.ndarray:
    """Lines through each point: the given size at every point, growing towards the middle of each interval."""
    points = sorted(set(points))
    lines = [points[0]]
    for a, b in zip(points, points[1:]):
        left, right, xa, xb, da, db = [], [], a, b, cell, cell
        while xb - xa > da + db:
            xa += da
            left.append(xa)
            da = min(da * ratio, largest)
            if xb - xa > da + db:
                xb -= db
                right.append(xb)
                db = min(db * ratio, largest)
        lines += left + right[::-1] + [b]
    return np.array(sorted(set(np.round(lines, 6))))


def _energy(x: np.ndarray, y: np.ndarray, fixed: np.ndarray, eps: np.ndarray) -> float:
    """Twice the stored energy per unit length for unit potential, in units of epsilon_0: the capacitance."""
    dx, dy = np.diff(x), np.diff(y)
    # Face conductances between neighbouring nodes, from the cells either side.
    gx = np.zeros((len(x) - 1, len(y)))
    gx[:, :-1] += eps * dy[None, :] / 2
    gx[:, 1:] += eps * dy[None, :] / 2
    gx /= dx[:, None]
    gy = np.zeros((len(x), len(y) - 1))
    gy[:-1, :] += eps * dx[:, None] / 2
    gy[1:, :] += eps * dx[:, None] / 2
    gy /= dy[None, :]
    free = fixed < 0
    index = -np.ones(fixed.shape, dtype=int)
    index[free] = np.arange(free.sum())
    a = np.concatenate([index[:-1, :].ravel(), index[:, :-1].ravel()])
    b = np.concatenate([index[1:, :].ravel(), index[:, 1:].ravel()])
    g = np.concatenate([gx.ravel(), gy.ravel()])
    pa = np.concatenate([fixed[:-1, :].ravel(), fixed[:, :-1].ravel()])
    pb = np.concatenate([fixed[1:, :].ravel(), fixed[:, 1:].ravel()])
    both = (a >= 0) & (b >= 0)
    n = int(free.sum())
    rows = np.concatenate([a[both], b[both], a[both], b[both]])
    cols = np.concatenate([b[both], a[both], a[both], b[both]])
    vals = np.concatenate([-g[both], -g[both], g[both], g[both]])
    rhs = np.zeros(n)
    only_a = (a >= 0) & (b < 0)
    only_b = (b >= 0) & (a < 0)
    rows = np.concatenate([rows, a[only_a], b[only_b]])
    cols = np.concatenate([cols, a[only_a], b[only_b]])
    vals = np.concatenate([vals, g[only_a], g[only_b]])
    np.add.at(rhs, a[only_a], g[only_a] * pb[only_a])
    np.add.at(rhs, b[only_b], g[only_b] * pa[only_b])
    solution = sparse_linalg.spsolve(sparse.csc_matrix((vals, (rows, cols)), shape=(n, n)), rhs)
    phi = fixed.copy()
    phi[free] = solution
    va = phi[np.concatenate([np.arange(fixed.shape[0] - 1).repeat(fixed.shape[1]), np.arange(fixed.shape[0]).repeat(fixed.shape[1] - 1)]),
             np.concatenate([np.tile(np.arange(fixed.shape[1]), fixed.shape[0] - 1), np.tile(np.arange(fixed.shape[1] - 1), fixed.shape[0])])]
    vb = phi[np.concatenate([np.arange(1, fixed.shape[0]).repeat(fixed.shape[1]), np.arange(fixed.shape[0]).repeat(fixed.shape[1] - 1)]),
             np.concatenate([np.tile(np.arange(fixed.shape[1]), fixed.shape[0] - 1), np.tile(np.arange(1, fixed.shape[1]), fixed.shape[0])])]
    return float(np.sum(g * (va - vb) ** 2))


def _solve(width: float, gap: float, copper: float, height: float, er: float, side: float, air: float, cell: float) -> tuple[float, float]:
    x = _graded([-width / 2 - gap - side, -width / 2 - gap, -width / 2, width / 2, width / 2 + gap, width / 2 + gap + side], cell)
    y = _graded([0.0, height, height + copper, height + copper + air], cell)
    yc = (y[1:] + y[:-1]) / 2
    fixed = np.full((len(x), len(y)), -1.0)
    fixed[:, 0] = 0.0                                  # the reference plane
    band = (y >= height - 1e-9) & (y <= height + copper + 1e-9)
    fixed[np.ix_(np.abs(x) <= width / 2 + 1e-9, band)] = 1.0
    if gap is not None:
        fixed[np.ix_(np.abs(x) >= width / 2 + gap - 1e-9, band)] = 0.0
    with_board = np.ones((len(x) - 1, len(y) - 1))
    with_board[:, yc < height] = er
    empty = np.ones_like(with_board)
    loaded, bare = _energy(x, y, fixed, with_board) * E0, _energy(x, y, fixed, empty) * E0
    return 1 / (C0 * math.sqrt(loaded * bare)), loaded / bare


def coplanar_z0(width_um: float, gap_um: float, copper_um: float, height_um: float, er: float, side_um: float = 3000.0,
                air_um: float = 3000.0, cells: tuple[float, ...] = (8.0, 4.0)) -> dict:
    """Z0 and effective permittivity of a coplanar line over a plane, with the last two grids extrapolated."""
    reads = [_solve(width_um, gap_um, copper_um, height_um, er, side_um, air_um, c) for c in cells]
    z = reads[-1][0] if len(reads) < 2 else 2 * reads[-1][0] - reads[-2][0]
    eps = reads[-1][1] if len(reads) < 2 else 2 * reads[-1][1] - reads[-2][1]
    return {"z0_ohm": round(z, 2), "eps_eff_static": round(eps, 3), "grid_change_ohm": round(abs(reads[-1][0] - reads[0][0]), 3)}
