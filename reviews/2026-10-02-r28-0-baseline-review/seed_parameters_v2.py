"""Sketch of seed-driven continuous task parameters for the product manifest (B-4).

Today tests/sim/product_cases.py derives case, direction, entry speed and offset from the seed
index with periods 3/4/3/4, so without late-tick injection the layer repeats after 144 seeds
(48 for drops).  The sketch keeps case and direction stratified by index (balanced counts) and
draws the continuous parameters from a per-task RNG seeded by "<layer>:<seed>"; Python's string
seeding hashes with SHA-512, so values are identical on every platform.

    python seed_parameters_v2.py        # prints distinct-configuration counts, old vs sketch
"""
import random

CASES = {"point": ["flat", "turn", "wall_detour"], "strict_drop": ["drop_2", "drop_5"]}
DIRECTIONS = ["south", "east", "north", "west"]


def old_parameters(family, index):
    cases = CASES[family]
    return (cases[index % len(cases)], DIRECTIONS[index % 4],
            [0.0, 1.0, 3.0][(index // len(cases)) % 3], [0.0, 0.05, 0.12, 0.30][(index // 4) % 4])


def new_parameters(layer, family, index):
    cases = CASES[family]
    rng = random.Random(f"{layer}:{index}")
    return (
        cases[index % len(cases)],
        DIRECTIONS[(index // len(cases)) % 4],
        round(rng.uniform(0.0, 3.0), 4),                                   # entry speed, blocks/s
        (round(rng.uniform(-0.3, 0.3), 4), round(rng.uniform(-0.3, 0.3), 4)),  # start offset x/z
        (round(rng.uniform(-0.45, 0.45), 4), round(rng.uniform(-0.45, 0.45), 4))  # goal jitter (after B-1)
        if family == "point" else (0.0, 0.0),
    )


for layer, family in (("point-normal", "point"), ("drop-normal", "strict_drop")):
    for n in (200, 1500):
        old = {old_parameters(family, i) for i in range(n)}
        new = {new_parameters(layer, family, i) for i in range(n)}
        print(f"{layer:<13} n={n:<5} distinct: current {len(old):>4}   sketch {len(new):>4}")
