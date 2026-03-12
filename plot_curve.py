import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
from itertools import product

# ---- Simulate 30 fluctuating curves ----
np.random.seed(42)
n_steps = 200
algos = [f'Algo-{i+1:02d}' for i in range(30)]

data = {}
for i, name in enumerate(algos):
    base = np.random.uniform(0.3, 0.9)
    trend = np.linspace(0, np.random.uniform(-0.1, 0.2), n_steps)
    noise = np.random.normal(0, np.random.uniform(0.02, 0.08), n_steps)
    # Add periodic oscillation
    wave = np.sin(np.linspace(0, np.random.uniform(2, 8) * np.pi, n_steps)) * np.random.uniform(0.01, 0.05)
    data[name] = base + trend + noise + wave

df = pd.DataFrame(data, index=range(n_steps))

# ---- Colorblind-friendly palette ----
# First 8 from Wong (2011) Nature Methods, rest from Tol palette
color_pool = [
    '#000000',  # black
    '#E69F00',  # orange
    '#56B4E9',  # sky blue
    '#009E73',  # teal
    '#F0E442',  # yellow
    '#0072B2',  # deep blue
    '#D55E00',  # vermilion
    '#CC79A7',  # pink-purple
    '#332288',  # indigo
    '#882255',  # wine
    '#44AA99',  # blue-green
    '#999933',  # olive
    '#AA4499',  # purple
    '#6699CC',  # steel blue
    '#661100',  # brown
]

linestyle_pool = ['-', '--']
marker_pool = ['o', 's', '^', 'D', 'v', 'P', 'X', '*', 'h', '<']

# 15 colors x 2 linestyles = 30 unique combos
combos = list(product(range(len(color_pool)), range(len(linestyle_pool))))

# Place a marker every N points to avoid clutter
MARK_EVERY = max(len(df) // 12, 1)

fig, ax = plt.subplots(figsize=(30, 14), dpi=150)

for i, col in enumerate(df.columns):
    ci, li = combos[i % len(combos)]
    mi = i % len(marker_pool)

    ax.plot(
        df.index, df[col],
        color=color_pool[ci],
        linestyle=linestyle_pool[li],
        linewidth=1.4,
        alpha=0.8,
        marker=marker_pool[mi],
        markersize=5,
        markevery=MARK_EVERY,
        markeredgewidth=0.6,
        markeredgecolor='white',
        label=col,
    )

# Legend on the right side, outside the plot area
ax.legend(
    fontsize=8,
    loc='center left',
    bbox_to_anchor=(1.01, 0.5),
    ncol=1,
    frameon=True,
    handlelength=3.5,
)

ax.set_xlabel('Epoch', fontsize=12)
ax.set_ylabel('Score', fontsize=12)
ax.tick_params(labelsize=10)
ax.grid(True, alpha=0.2)
ax.spines['top'].set_visible(False)
ax.spines['right'].set_visible(False)

plt.savefig('algo_comparison.png', bbox_inches='tight', dpi=150)
plt.close()
print('Done')