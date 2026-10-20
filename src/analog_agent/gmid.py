"""Technology-independent, local-only gm/Id tables. All quantities use SI units."""
from __future__ import annotations
import argparse
import csv
import itertools
import json
import math
from pathlib import Path

import numpy as np
from scipy.interpolate import RegularGridInterpolator

AXES = ('l', 'vds', 'vsb', 'vgs')
REQUIRED = ('model', 'corner', 'temp_c', *AXES, 'id', 'gm', 'gds', 'cgg', 'w_ref')


class LookupTable:
    """Rectangular grids per (model, corner, temperature); no extrapolation."""
    def __init__(self, path):
        self.groups = {}
        self.missing_context = []
        with Path(path).open(encoding='utf-8-sig', newline='') as stream:
            reader = csv.DictReader(stream)
            if not set(REQUIRED) <= set(reader.fieldnames or []):
                raise ValueError('Missing required CSV columns: ' + ', '.join(REQUIRED))
            numeric = [n for n in reader.fieldnames if n not in ('model', 'corner')]
            raw_rows = list(reader)
            for name in ('temp_c', 'vds', 'vsb'):
                blanks = [not r[name].strip() for r in raw_rows]
                if any(blanks):
                    if not all(blanks):
                        raise ValueError('Context column must be entirely known or entirely blank')
                    self.missing_context.append(name)
            for raw in raw_rows:
                row = {n: (0.0 if n in self.missing_context else float(raw[n])) for n in numeric}
                if not all(math.isfinite(v) for v in row.values()):
                    raise ValueError('Nonfinite table value')
                if any(row[n] <= 0 for n in ('l', 'id', 'gm', 'cgg', 'w_ref')) or row['gds'] < 0:
                    raise ValueError('Expected positive magnitudes and geometry; gds must be nonnegative')
                if not raw['model'] or not raw['corner']:
                    raise ValueError('Empty model or corner')
                self.groups.setdefault((raw['model'], raw['corner'], row['temp_c']), []).append(row)
        if not self.groups:
            raise ValueError('Empty table')
        self.tables = {}
        for key, rows in self.groups.items():
            axes = [np.array(sorted({r[a] for r in rows})) for a in AXES]
            points = {tuple(r[a] for a in AXES): r for r in rows}
            if len(points) != len(rows) or len(points) != math.prod(map(len, axes)):
                raise ValueError('Duplicate points or incomplete rectangular grid')
            fields = {}
            for name in numeric:
                values = np.array([points[p][name] for p in itertools.product(*axes)]).reshape(tuple(map(len, axes)))
                fields[name] = RegularGridInterpolator(axes, values, bounds_error=True)
            self.tables[key] = (axes, fields)

    def query(self, *, model, corner, l, vgs, temp_c=None, vds=None, vsb=None):
        context = self._context(temp_c=temp_c, vds=vds, vsb=vsb)
        temp_c, vds, vsb = (context[n] for n in ('temp_c', 'vds', 'vsb'))
        try:
            _, fields = self.tables[(model, corner, float(temp_c))]
        except KeyError:
            raise ValueError('Unknown model/corner/temperature group') from None
        point = [float(l), float(vds), float(vsb), float(vgs)]
        if not all(math.isfinite(v) for v in point):
            raise ValueError('Nonfinite query')
        result = {n: float(f([point])[0]) for n, f in fields.items()}
        result.update(gm_id=result['gm']/result['id'], id_w=result['id']/result['w_ref'],
                      ft=result['gm']/(2*math.pi*result['cgg']))
        if result['gds'] > 0:
            result['gm_gds'] = result['gm']/result['gds']
        for name in self.missing_context:
            result.pop(name, None)
        if self.missing_context:
            result['missing_context'] = list(self.missing_context)
        return result

    def _context(self, **values):
        for name, value in values.items():
            if name in self.missing_context:
                if value is not None:
                    raise ValueError(f'{name} is not characterized; omit it rather than inventing a value')
                values[name] = 0.0  # internal index only; never reported as a measured condition
            elif value is None:
                raise ValueError(f'{name} is required for this table')
        return values

    def at_gm_id(self, target, *, branch=None, **bias):
        """Invert gm/Id along VGS using interpolated primitive gm and Id.

        Nonmonotonic curves require an explicit zero-based root branch.
        """
        target = float(target)
        if not math.isfinite(target) or target <= 0:
            raise ValueError('gm_id must be finite and positive')
        context = self._context(**{n: bias.get(n) for n in ('temp_c', 'vds', 'vsb')})
        key = (bias['model'], bias['corner'], float(context['temp_c']))
        if key not in self.tables:
            raise ValueError('Unknown model/corner/temperature group')
        grid = self.tables[key][0][-1]
        rows = [self.query(vgs=v, **bias) for v in grid]
        errors = [r['gm'] - target*r['id'] for r in rows]
        roots = []
        for i, (v, error) in enumerate(zip(grid, errors)):
            if error == 0:
                roots.append(float(v))
            if i+1 < len(grid):
                next_error = errors[i+1]
                if error == 0 and next_error == 0:
                    raise ValueError('gm_id plateau has no unique inverse')
                if error*next_error < 0:
                    roots.append(float(v + (grid[i+1]-v)*(-error)/(next_error-error)))
        roots = sorted(set(roots))
        if not roots:
            raise ValueError('gm_id outside characterized range')
        if branch is None and len(roots) != 1:
            raise ValueError('Multiple VGS roots; specify branch')
        index = 0 if branch is None else branch
        if not isinstance(index, int) or index < 0 or index >= len(roots):
            raise ValueError('Invalid inverse branch')
        vgs = roots[index]
        return dict(self.query(vgs=vgs, **bias), vgs=vgs)

    def size(self, *, gm_id, current, branch=None, **bias):
        current = float(current)
        if not math.isfinite(current) or current <= 0:
            raise ValueError('current must be finite and positive')
        result = self.at_gm_id(gm_id, branch=branch, **bias)
        result.update(width=current/result['id_w'], current=current,
                      gm_target=current*gm_id)
        return result


class SizingObjective:
    """scipy/TuRBO callable: [gm_id, length, current] -> finite loss.

    score receives LUT estimates; these are not verified circuit results.
    """
    def __init__(self, table, bias, score, penalty=1e12):
        self.table, self.bias, self.score = table, dict(bias), score
        self.penalty = float(penalty)
        if not math.isfinite(self.penalty):
            raise ValueError('Penalty must be finite')

    def __call__(self, x):
        try:
            gm_id, length, current = map(float, x)
            result = self.table.size(gm_id=gm_id, current=current, **dict(self.bias, l=length))
            value = float(self.score(result))
            return value if math.isfinite(value) else self.penalty
        except (ValueError, KeyError, ArithmeticError):
            return self.penalty


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('table', type=Path)
    parser.add_argument('request', type=Path, help='JSON containing mode and query arguments')
    args = parser.parse_args()
    request = json.loads(args.request.read_text(encoding='utf-8-sig'))
    mode = request.pop('mode', 'query')
    if mode not in ('query', 'at_gm_id', 'size'):
        parser.error('mode must be query, at_gm_id, or size')
    print(json.dumps(getattr(LookupTable(args.table), mode)(**request), allow_nan=False, indent=2))


if __name__ == '__main__':
    main()
