"""Generate ADO sizing seeds from private gm/Id requests; never runs simulations."""
from pathlib import Path
from ..core import load_config, local_path
from ..parameters import validate_values
from ..gmid import LookupTable


def propose(context):
    cfg = load_config(Path(context['project_config']))
    # Private override is merged by load_config; never echo the table or requests.
    settings = cfg['campaign']['gmid']
    root = Path(cfg['_project_root'])
    table_path = local_path(root, settings['table'])
    private = (root / 'private').resolve()
    if not table_path.is_relative_to(private):
        raise ValueError('Process tables must be under project private/')
    table = LookupTable(table_path)
    if table.missing_context and not settings.get('allow_incomplete_context', False):
        raise ValueError('Incomplete characterization context; enable allow_incomplete_context only for lookup validation')
    points = []
    for i, request in enumerate(settings['requests']):
        result = table.size(**request['sizing'])
        parameters = dict(request.get('parameters', {}))
        for output, parameter in settings['outputs'].items():
            if output == 'l':
                parameters[parameter] = request['sizing']['l']
            else:
                parameters[parameter] = result[output]
        parameters = validate_values(parameters, cfg.get('parameters', {}))
        points.append({'id': f'gmid-{context["cycle_count"]:04d}-{i:04d}',
                       'parameters': parameters,
                       'rationale': 'LUT sizing seed; requires simulation and analysis review'})
    return {'points': points, 'meta': {'source': 'local-gmid', 'verified_electrical_results': False}}
