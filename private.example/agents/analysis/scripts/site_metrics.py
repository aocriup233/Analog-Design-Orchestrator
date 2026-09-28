"""Example circuit-specific metric hook; copy into private or a project package."""


def evaluate(data, definition, run_dir):
    raise NotImplementedError("Return one measured scalar from verified simulator data")
