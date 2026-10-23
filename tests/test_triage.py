import pytest
from src.models import FailureDomain, SeverityLevel
from run_triage import ATETriageAgent, SyntheticDataGenerator

@pytest.fixture
def agent():
    return ATETriageAgent()

def test_socket_wear_guard(agent):
    log = SyntheticDataGenerator.generate_socket_wear_log(site=3)
    res = agent.triage_log(log)
    assert res.failure_domain == FailureDomain.FIXTURE_INTERMITTENT
    assert any("GUARD_CONTACT_RESISTANCE" in g for g in res.physical_guards_triggered)
    assert res.severity == SeverityLevel.HIGH

def test_thermal_excursion_guard(agent):
    log = SyntheticDataGenerator.generate_thermal_excursion_log(site=1)
    res = agent.triage_log(log)
    assert res.failure_domain == FailureDomain.THERMAL_EXCURSION
    assert res.severity == SeverityLevel.CRITICAL
    assert any("GUARD_THERMAL" in g for g in res.physical_guards_triggered)

def test_silicon_gate_fault(agent):
    log = SyntheticDataGenerator.generate_silicon_gate_fault_log(site=2)
    res = agent.triage_log(log)
    assert res.failure_domain == FailureDomain.SILICON_LOGIC
    assert len(res.physical_guards_triggered) == 0

def test_power_droop_guard(agent):
    log = SyntheticDataGenerator.generate_power_droop_log(site=4)
    res = agent.triage_log(log)
    assert res.failure_domain == FailureDomain.POWER_DELIVERY_DROOP
    assert any("GUARD_VDD_DROOP" in g for g in res.physical_guards_triggered)
