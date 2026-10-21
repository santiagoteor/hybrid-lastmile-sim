"""Two-stage M6 assignment: customer PUDO choice, then HD microhub allocation."""
from dataclasses import dataclass, asdict
from math import exp, log, isfinite
from random import Random
from hybrid_lastmile.core.scenario import Scenario

@dataclass(frozen=True)
class Allocation:
    customer_id: str
    packages: int
    choice: str
    delivery_mode: str
    facility_id: str | None
    network_distance_m: float | None
    pudo_probability: float


def probability(distance_m: float, scenario: Scenario) -> float:
    p = scenario.baseline_pudo_probability
    z = log(p/(1-p)) - scenario.distance_penalty_per_km*distance_m/1000
    return 1/(1+exp(-z)) if z >= 0 else exp(z)/(1+exp(z))


def _ordered_candidates(row, sites, remaining, packages, max_distance):
    choices = []
    for index, value in enumerate(row):
        distance = float(value)
        if isfinite(distance) and 0 <= distance <= max_distance and remaining[sites[index].site_id] >= packages:
            choices.append((distance,sites[index].site_id))
    return sorted(choices)


# Bound peak matrix memory without changing GLIMS/OSRM routing or candidate rules.
# A multiple of common OSRM block sizes avoids unnecessarily tiny requests.
_MATRIX_SOURCE_BATCH = 1000

def _distance_rows_only(origins, destinations, network, mode):
    """Yield distance rows in original order, using bounded rectangular matrices."""
    for start in range(0, len(origins), _MATRIX_SOURCE_BATCH):
        batch = origins[start:start + _MATRIX_SOURCE_BATCH]
        matrix = network.rectangular(batch, destinations, mode).distances_m
        if matrix.shape != (len(batch), len(destinations)):
            raise ValueError(f"Invalid {mode} matrix shape: {matrix.shape}")
        yield from matrix

def _distance_rows(origins, destinations, network, mode):
    return zip(origins, _distance_rows_only(origins, destinations, network, mode))


def simulate(customers, sites, network, scenario: Scenario):
    """Return (allocations, summary); exact OSRM matrices in bounded source batches.

    Choices are sampled once per customer; if a chosen PUDO fills, next feasible
    PUDO is considered. HD is assigned to a microhub only within cycling coverage.
    """
    if not customers:
        raise ValueError('No customers')
    customers = sorted(customers, key=lambda c: c.customer_id)
    if len({c.customer_id for c in customers}) != len(customers):
        raise ValueError('Duplicate customer IDs')
    pudos = sorted(sites['PUDO'], key=lambda s:s.site_id)
    microhubs = sorted(sites['MICROHUB'], key=lambda s:s.site_id)
    if not pudos:
        raise ValueError('No PUDO candidates')
    rng = Random(scenario.seed)
    pudo_remaining = {p.site_id:scenario.pudo_capacity for p in pudos}
    allocations = []
    hd_customers = []
    for customer, walking_row in _distance_rows(customers, pudos, network, "walking"):
        options = _ordered_candidates(walking_row,pudos,pudo_remaining,customer.packages,scenario.max_walking_distance_m)
        if options:
            dist, facility = options[0]
            p = probability(dist,scenario)
            if rng.random() < p:
                pudo_remaining[facility] -= customer.packages
                allocations.append(Allocation(customer.customer_id,customer.packages,'PUDO','PUDO',facility,dist,p))
                continue
        else:
            p = 0.0
        hd_customers.append((customer,p))
    micro_remaining = {m.site_id:scenario.microhub_capacity for m in microhubs}
    hd_only = [customer for customer,_ in hd_customers]
    for (customer,p), cycling_row in zip(hd_customers, _distance_rows_only(hd_only, microhubs, network, "cycling")):
        options = _ordered_candidates(cycling_row,microhubs,micro_remaining,customer.packages,scenario.microhub_max_cycling_distance_m)
        if options:
            distance,facility = options[0]
            micro_remaining[facility] -= customer.packages
            allocations.append(Allocation(customer.customer_id,customer.packages,'HD','CARGO_BIKE',facility,distance,p))
        else:
            allocations.append(Allocation(customer.customer_id,customer.packages,'HD','VAN',None,None,p))
    allocations.sort(key=lambda a:a.customer_id)
    summary = {'city':scenario.city,'customers':len(customers),'packages':sum(a.packages for a in allocations),
               'customers_by_mode':{mode:sum(a.delivery_mode==mode for a in allocations) for mode in ('PUDO','CARGO_BIKE','VAN')},
               'packages_by_mode':{mode:sum(a.packages for a in allocations if a.delivery_mode==mode) for mode in ('PUDO','CARGO_BIKE','VAN')},
               'pudo_remaining_capacity':pudo_remaining,'microhub_remaining_capacity':micro_remaining,
               'note':'Assignments only; vehicle routes, cost and emissions are not calculated.'}
    return allocations,summary
