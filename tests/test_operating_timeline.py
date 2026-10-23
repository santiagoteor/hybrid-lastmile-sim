from datetime import datetime, timedelta
import pytest
from hybrid_lastmile.workflow.operating_timeline import _config

def test_operating_hours_glims_defaults():
    start, end, margin, prep, service = _config({"simulation_date": "2026-10-23"})
    assert start == datetime(2026, 10, 23, 9)
    assert end == datetime(2026, 10, 23, 17)
    assert end - timedelta(minutes=margin) == datetime(2026, 10, 23, 16, 30)
    assert prep == {"supply": 15, "microhub_supply": 15, "bike_delivery": 5, "van_delivery": 15}
    assert service == 5

def test_margin_must_fit_shift():
    with pytest.raises(ValueError, match="smaller"):
        _config({"simulation_date": "2026-10-23", "last_service_margin_minutes": 480})

def test_date_required():
    with pytest.raises(ValueError, match="simulation_date"):
        _config({"start": "09:00"})


def test_glims_osrm_durations_are_already_minutes():
    import numpy as np
    from hybrid_lastmile.workflow.operating_timeline import _diagonal_edge_minutes
    # 29 km can take 40+ minutes, not 40 seconds. No second /60.
    durations_min = np.array([[41.5, 999.0], [999.0, 17.25]])
    assert _diagonal_edge_minutes(durations_min) == [41.5, 17.25]
