#!/usr/bin/env python3
"""
ATE Telemetry Triage Agent (Post-Silicon Validation & Burn-In Failure Analysis)
Author: David Rainey (MS ECE, Former Intel Product Development Engineer)
"""

import os
import re
import sys
import json
import logging
from typing import List, Optional, Dict, Any, Tuple
from src.models import FailureDomain, SeverityLevel, ParsedTelemetry, TriageAssessment

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("ate_triage")


class PhysicalLimitsValidator:
    """
    Physical sanity verification layer.
    Ensures that empirical silicon and test-fixture physical bounds are never
    compromised by probabilistic LLM outputs.
    """
    MAX_ALLOWABLE_TJ_C = 105.0
    CRITICAL_THERMAL_EXCURSION_C = 118.0
    NOMINAL_CONTACT_RESISTANCE_MOHM = 150.0
    DEGRADED_SOCKET_THRESHOLD_MOHM = 450.0
    MAX_PERMISSIBLE_VDD_DROOP_PERCENT = 8.5

    @classmethod
    def apply_guards(cls, telemetry: ParsedTelemetry, initial_domain: FailureDomain) -> Tuple[FailureDomain, List[str], SeverityLevel]:
        guards: List[str] = []
        final_domain = initial_domain
        severity = SeverityLevel.MEDIUM

        if telemetry.peak_junction_temp_c >= cls.CRITICAL_THERMAL_EXCURSION_C:
            guards.append(
                f"GUARD_THERMAL: Peak Tj ({telemetry.peak_junction_temp_c}°C) exceeded critical limit ({cls.CRITICAL_THERMAL_EXCURSION_C}°C). "
                f"Forcing classification to THERMAL_EXCURSION."
            )
            final_domain = FailureDomain.THERMAL_EXCURSION
            severity = SeverityLevel.CRITICAL

        elif telemetry.contact_resistance_mohm >= cls.DEGRADED_SOCKET_THRESHOLD_MOHM:
            guards.append(
                f"GUARD_CONTACT_RESISTANCE: Measured contact resistance ({telemetry.contact_resistance_mohm} mΩ) "
                f"exceeds socket health threshold ({cls.DEGRADED_SOCKET_THRESHOLD_MOHM} mΩ). "
                f"Forcing classification to FIXTURE_INTERMITTENT before condemning silicon."
            )
            final_domain = FailureDomain.FIXTURE_INTERMITTENT
            severity = SeverityLevel.HIGH

        else:
            droop_pct = ((telemetry.nominal_vdd_v - telemetry.minimum_vdd_v) / telemetry.nominal_vdd_v) * 100.0
            if droop_pct >= cls.MAX_PERMISSIBLE_VDD_DROOP_PERCENT:
                guards.append(
                    f"GUARD_VDD_DROOP: Measured transient supply droop of {droop_pct:.1f}% exceeds maximum allowable "
                    f"budget ({cls.MAX_PERMISSIBLE_VDD_DROOP_PERCENT}%). Flagging as POWER_DELIVERY_DROOP."
                )
                final_domain = FailureDomain.POWER_DELIVERY_DROOP
                severity = SeverityLevel.HIGH

        return final_domain, guards, severity


class ATELogParser:
    """Deterministic regex extraction engine for standard ATE execution logs."""

    SITE_REGEX = re.compile(r"(?:SITE|SOCKET|DUT_ID)[:\s]+(\d+)", re.IGNORECASE)
    PHASE_REGEX = re.compile(r"(?:PHASE|TEST_MODULE|TEST_SUITE)[:\s]+([\w\-_]+)", re.IGNORECASE)
    TEMP_PEAK_REGEX = re.compile(r"(?:TJ_PEAK|PEAK_TEMP|TJMAX)[:\s]+([0-9.]+)\s*C?", re.IGNORECASE)
    TEMP_BASE_REGEX = re.compile(r"(?:TJ_BASE|CHUCK_TEMP|AMBIENT)[:\s]+([0-9.]+)\s*C?", re.IGNORECASE)
    VDD_NOM_REGEX = re.compile(r"(?:VDD_NOM|VDD_SET|V_FORCE)[:\s]+([0-9.]+)\s*V?", re.IGNORECASE)
    VDD_MIN_REGEX = re.compile(r"(?:VDD_MIN|V_SENSE_MIN|VDD_DROOP_VAL)[:\s]+([0-9.]+)\s*V?", re.IGNORECASE)
    RCONT_REGEX = re.compile(r"(?:R_CONT|CONTACT_RES|PIN_RES)[:\s]+([0-9.]+)\s*m?Ohm", re.IGNORECASE)
    CYCLE_REGEX = re.compile(r"(?:FAIL_CYCLE|VECTOR_OFFSET|ERROR_STROBE)[:\s]+(\d+)", re.IGNORECASE)
    FAIL_PIN_REGEX = re.compile(r"(?:FAIL_PINS|CHANNELS|MISMATCH_PINS)[:\s]+\[?([A-Za-z0-9_,\s]+)\]?", re.IGNORECASE)
    REG_DUMP_REGEX = re.compile(r"(0x[0-9A-Fa-f]{4,8})\s*[:=]\s*(0x[0-9A-Fa-f]{4,8})")

    @classmethod
    def parse_log(cls, raw_log_text: str) -> ParsedTelemetry:
        site_match = cls.SITE_REGEX.search(raw_log_text)
        site_id = int(site_match.group(1)) if site_match else 1

        phase_match = cls.PHASE_REGEX.search(raw_log_text)
        test_phase = phase_match.group(1).upper() if phase_match else "ATPG_BURST"

        peak_t = cls.TEMP_PEAK_REGEX.search(raw_log_text)
        peak_temp = float(peak_t.group(1)) if peak_t else 85.0

        base_t = cls.TEMP_BASE_REGEX.search(raw_log_text)
        base_temp = float(base_t.group(1)) if base_t else 70.0

        vdd_nom = cls.VDD_NOM_REGEX.search(raw_log_text)
        nominal_vdd = float(vdd_nom.group(1)) if vdd_nom else 1.05

        vdd_min = cls.VDD_MIN_REGEX.search(raw_log_text)
        minimum_vdd = float(vdd_min.group(1)) if vdd_min else nominal_vdd

        rcont = cls.RCONT_REGEX.search(raw_log_text)
        contact_res = float(rcont.group(1)) if rcont else 115.0

        cycle = cls.CYCLE_REGEX.search(raw_log_text)
        err_cycle = int(cycle.group(1)) if cycle else None

        pins_match = cls.FAIL_PIN_REGEX.search(raw_log_text)
        failing_pins = []
        if pins_match:
            raw_pins = pins_match.group(1).replace(" ", "")
            failing_pins = [p for p in raw_pins.split(",") if p]

        reg_matches = cls.REG_DUMP_REGEX.findall(raw_log_text)
        reg_dict = {k: v for k, v in reg_matches}

        return ParsedTelemetry(
            site_id=site_id,
            test_phase=test_phase,
            peak_junction_temp_c=peak_temp,
            baseline_temp_c=base_temp,
            nominal_vdd_v=nominal_vdd,
            minimum_vdd_v=minimum_vdd,
            contact_resistance_mohm=contact_res,
            error_cycle_offset=err_cycle,
            failing_pins=failing_pins,
            register_signature=reg_dict
        )


class ATETriageAgent:
    """Automated diagnostic engine."""

    def triage_log(self, raw_log_text: str) -> TriageAssessment:
        telemetry = ATELogParser.parse_log(raw_log_text)
        preliminary_domain = self._preliminary_classify(telemetry, raw_log_text)
        validated_domain, triggered_guards, severity = PhysicalLimitsValidator.apply_guards(
            telemetry, preliminary_domain
        )
        return self._synthesize_assessment(
            telemetry=telemetry,
            domain=validated_domain,
            severity=severity,
            guards=triggered_guards,
            raw_log=raw_log_text
        )

    def _preliminary_classify(self, telemetry: ParsedTelemetry, raw_log: str) -> FailureDomain:
        log_lower = raw_log.lower()
        if "scan_stuck_at" in log_lower or "atpg_vector_mismatch" in log_lower:
            return FailureDomain.SILICON_LOGIC
        elif "strobe_jitter" in log_lower or "timing_deskew" in log_lower:
            return FailureDomain.TESTER_HARNESS_SYNCH
        elif "overtemp" in log_lower or "thermal_trip" in log_lower:
            return FailureDomain.THERMAL_EXCURSION
        elif "droop" in log_lower or "undervolt" in log_lower:
            return FailureDomain.POWER_DELIVERY_DROOP
        elif "pogo" in log_lower or "socket_cont" in log_lower:
            return FailureDomain.FIXTURE_INTERMITTENT
        return FailureDomain.SILICON_LOGIC

    def _synthesize_assessment(
        self,
        telemetry: ParsedTelemetry,
        domain: FailureDomain,
        severity: SeverityLevel,
        guards: List[str],
        raw_log: str
    ) -> TriageAssessment:
        evidence: List[str] = []
        for line in raw_log.strip().splitlines():
            line_str = line.strip()
            if any(term in line_str.upper() for term in ["FAIL", "ERROR", "TJ", "R_CONT", "VDD", "MISMATCH", "0X"]):
                evidence.append(line_str)
        if not evidence:
            evidence = ["General failure reported during execution window."]

        if domain == FailureDomain.FIXTURE_INTERMITTENT:
            signature = f"Site {telemetry.site_id}: Socket Contact Resistance Degraded ({telemetry.contact_resistance_mohm:.1f} mΩ) causing false vector fail."
            actions = [
                f"Inspect and clean test socket pogo pins at Site {telemetry.site_id} using cleanroom isopropyl alcohol swab.",
                "Perform 4-wire Kelvin resistance loopback check across DUT interface board (DIB) channels.",
                "Re-test device in adjacent calibrated socket (e.g., Site 2) to verify repeatability."
            ]
            unblock = "Socket maintenance or DIB swap required; do NOT scrap silicon."

        elif domain == FailureDomain.THERMAL_EXCURSION:
            signature = f"Site {telemetry.site_id}: Thermal Excursion Event (Tj reached {telemetry.peak_junction_temp_c:.1f}°C, limit {PhysicalLimitsValidator.MAX_ALLOWABLE_TJ_C}°C)."
            actions = [
                "Inspect thermal interface material (TIM) or thermal chuck pneumatic pressure.",
                "Verify chiller supply liquid flow rate and inlet temperature calibration.",
                "Rerun low-stress parametric continuity check before re-engaging burn-in stress."
            ]
            unblock = "Restore thermal cooling loop pressure and inspect package for thermal delamination."

        elif domain == FailureDomain.POWER_DELIVERY_DROOP:
            droop = ((telemetry.nominal_vdd_v - telemetry.minimum_vdd_v) / telemetry.nominal_vdd_v) * 100.0
            signature = f"Site {telemetry.site_id}: VDD Supply Rail Droop ({droop:.1f}% drop under vector execution)."
            actions = [
                "Probe VDD core rail at DIB decoupling capacitors using high-bandwidth oscilloscope (≥1 GHz).",
                "Audit local ceramic bypass capacitors on tester load board for cracked solder joints.",
                "Increase test vector warm-up idle cycles to dampen di/dt current step."
            ]
            unblock = "Adjust dynamic power supply programmable slew rate or inspect load-board decoupling."

        elif domain == FailureDomain.TESTER_HARNESS_SYNCH:
            signature = f"Site {telemetry.site_id}: Channel Timing Deskew / Strobe Mismatch on pins: {telemetry.failing_pins}."
            actions = [
                "Execute automated ATE digital timing deskew and pin calibration routine (T-Cal).",
                "Measure drive-edge delay vs. receive comparator strobe on high-speed pin channels.",
                "Inspect coaxial test-head cable connectors for loose SMA fittings."
            ]
            unblock = "Rerun pin electronic calibration routine to realign strobe timing."

        else:
            pins_str = ", ".join(telemetry.failing_pins) if telemetry.failing_pins else "Internal Chain"
            cycle_str = f"cycle {telemetry.error_cycle_offset}" if telemetry.error_cycle_offset else "multiple offsets"
            signature = f"Site {telemetry.site_id}: Deterministic Silicon Gate/Chain Fault in {telemetry.test_phase} at {cycle_str} (Pins: {pins_str})."
            actions = [
                f"Capture scan-chain bit dump at error cycle {telemetry.error_cycle_offset} for CAD defect localization.",
                "Rerun ATPG vector suite at lower clock frequency (TTR trade-off) to isolate timing marginality from hard stuck-at fault.",
                "Route device to Physical Failure Analysis (FA) lab for optical emission microscopy (PEM) or laser voltage probing."
            ]
            unblock = "Silicon defect confirmed; log failure signature into STDF yield database and index for die stepping review."

        confidence = 0.95 if guards else 0.88

        return TriageAssessment(
            failure_domain=domain,
            severity=severity,
            confidence_score=confidence,
            physical_guards_triggered=guards,
            primary_failure_signature=signature,
            evidence_snippets=evidence[:5],
            recommended_bench_actions=actions,
            unblock_pathway=unblock
        )


class SyntheticDataGenerator:
    """Generates authentic ATE burn-in logs modeling 4 distinct post-silicon failure modes."""

    @staticmethod
    def generate_socket_wear_log(site: int = 3) -> str:
        return f"""================================================================================
ATE BURN-IN SYSTEM LOG - LOT_ID: INTL_7320_W04 - DATE: 2026-10-20
================================================================================
DUT_ID: {site} | SOCKET: {site} | DIB_REV: 3.2 | TESTER_HEAD: TH-02
TEST_MODULE: ATPG_BURST_STRESS | PATTERN_SET: CORE_TILES_SCAN_REV4
CHUCK_TEMP: 72.0 C | TJ_BASE: 74.5 C | TJ_PEAK: 84.1 C
VDD_NOM: 1.05 V | V_FORCE: 1.050 V | V_SENSE_MIN: 1.032 V
R_CONT: 512.4 mOhm | CONTACT_LIMIT: 250.0 mOhm [WARNING: CONTACT DRIFT]
VECTOR_OFFSET: 419204 | ERROR_STROBE: CYCLE_419204
CHANNELS: [PIN_CLK_IN, PIN_SCAN_OUT_4]
STATUS: FAIL_VECTOR_MISMATCH
REGISTER_DUMPS:
  0x0040_0010 = 0x0000_0001
  0x0040_0014 = 0xDEAD_BEEF
  0x0080_0028 = 0x0000_03F4
DIAGNOSTIC_MSG: Vector mismatch on PIN_SCAN_OUT_4 at cycle 419204. Repeat count = 3.
================================================================================
"""

    @staticmethod
    def generate_thermal_excursion_log(site: int = 1) -> str:
        return f"""================================================================================
ATE BURN-IN SYSTEM LOG - LOT_ID: INTL_7320_W04 - DATE: 2026-10-20
================================================================================
DUT_ID: {site} | SOCKET: {site} | DIB_REV: 3.2 | TESTER_HEAD: TH-01
TEST_MODULE: HTOL_BURN_IN_STRESS | PATTERN_SET: MAX_POWER_DYN_LOAD
CHUCK_TEMP: 85.0 C | TJ_BASE: 88.0 C | TJMAX: 124.6 C [CRITICAL OVERTEMP]
VDD_NOM: 1.20 V | V_FORCE: 1.200 V | VDD_MIN: 1.160 V
R_CONT: 120.5 mOhm
FAIL_CYCLE: 18400
FAIL_PINS: [PIN_TH_ALARM, PIN_PWR_GOOD]
STATUS: EMERGENCY_THERMAL_SHUTDOWN_ASSERTED
REGISTER_DUMPS:
  0x0010_0004 = 0x8000_00FF
  0x0010_0008 = 0x0000_007C
DIAGNOSTIC_MSG: Junction sensor crossed safety trip (124.6 C > 105.0 C). Power kill fired.
================================================================================
"""

    @staticmethod
    def generate_silicon_gate_fault_log(site: int = 2) -> str:
        return f"""================================================================================
ATE BURN-IN SYSTEM LOG - LOT_ID: INTL_7320_W04 - DATE: 2026-10-20
================================================================================
DUT_ID: {site} | SOCKET: {site} | DIB_REV: 3.2 | TESTER_HEAD: TH-01
TEST_MODULE: SCAN_STUCK_AT | PATTERN_SET: L2_CACHE_TAG_SCAN
CHUCK_TEMP: 65.0 C | TJ_BASE: 66.0 C | TJ_PEAK: 71.2 C
VDD_NOM: 0.95 V | V_FORCE: 0.950 V | VDD_MIN: 0.941 V
R_CONT: 110.2 mOhm
VECTOR_OFFSET: 1048576 | ERROR_STROBE: STUCK_AT_0
FAIL_PINS: [PIN_SCAN_OUT_12]
STATUS: FAIL_PATTERN_DATA_MISMATCH
REGISTER_DUMPS:
  0x0020_0100 = 0x0000_0000
  0x0020_0104 = 0xFFFF_0000
DIAGNOSTIC_MSG: SCAN stuck-at-0 verified across 5 consecutive pattern restarts. Clean fixture.
================================================================================
"""

    @staticmethod
    def generate_power_droop_log(site: int = 4) -> str:
        return f"""================================================================================
ATE BURN-IN SYSTEM LOG - LOT_ID: INTL_7320_W04 - DATE: 2026-10-20
================================================================================
DUT_ID: {site} | SOCKET: {site} | DIB_REV: 3.2 | TESTER_HEAD: TH-02
TEST_MODULE: ALL_CORES_ATPG_MAX | PATTERN_SET: DI_DT_STRESS_V2
CHUCK_TEMP: 75.0 C | TJ_BASE: 76.0 C | TJ_PEAK: 89.4 C
VDD_NOM: 1.10 V | V_FORCE: 1.100 V | VDD_DROOP_VAL: 0.985 V
R_CONT: 118.0 mOhm
FAIL_CYCLE: 82910
FAIL_PINS: [PIN_CORE_PLL_LOCK, PIN_BUS_VALID]
STATUS: FAIL_VOLTAGE_DROOP_TIMEOUT
REGISTER_DUMPS:
  0x0030_0010 = 0x0000_0040
DIAGNOSTIC_MSG: Core PLL lost lock during di/dt current burst. Supply droop: 10.5%.
================================================================================
"""


def print_triage_card(assessment: TriageAssessment):
    domain_colors = {
        FailureDomain.SILICON_LOGIC: "\033[91m",
        FailureDomain.FIXTURE_INTERMITTENT: "\033[93m",
        FailureDomain.THERMAL_EXCURSION: "\033[95m",
        FailureDomain.POWER_DELIVERY_DROOP: "\033[96m",
        FailureDomain.TESTER_HARNESS_SYNCH: "\033[94m",
    }
    reset = "\033[0m"
    bold = "\033[1m"
    color = domain_colors.get(assessment.failure_domain, reset)

    print("\n" + "=" * 78)
    print(f"{bold}POST-SILICON ATE TELEMETRY TRIAGE REPORT{reset}")
    print("=" * 78)
    print(f"  {bold}CLASSIFICATION:{reset}  {color}{assessment.failure_domain.value}{reset}")
    print(f"  {bold}SEVERITY:{reset}        {assessment.severity.value} | Confidence: {assessment.confidence_score * 100:.1f}%")
    print(f"  {bold}SIGNATURE:{reset}       {assessment.primary_failure_signature}")
    print("-" * 78)

    if assessment.physical_guards_triggered:
        print(f"  {bold}\033[93m[!] PHYSICAL BOUNDARY GUARDS TRIGGERED:{reset}")
        for guard in assessment.physical_guards_triggered:
            print(f"      • {guard}")
        print("-" * 78)

    print(f"  {bold}EVIDENCE LOG SNIPPETS:{reset}")
    for snippet in assessment.evidence_snippets:
        print(f"      | {snippet}")
    print("-" * 78)

    print(f"  {bold}RECOMMENDED BENCH ACTIONS (PRIORITIZED):{reset}")
    for i, action in enumerate(assessment.recommended_bench_actions, 1):
        print(f"      {i}. {action}")
    print("-" * 78)

    print(f"  {bold}UNBLOCK PATHWAY:{reset} \033[92m{assessment.unblock_pathway}{reset}")
    print("=" * 78 + "\n")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="ATE Telemetry Triage Agent")
    parser.add_argument("--demo", action="store_true", help="Run full 4-scenario demonstration")
    parser.add_argument("--file", type=str, help="Path to ATE log file to triage")
    parser.add_argument("--json", action="store_true", help="Output assessment as raw JSON")

    args = parser.parse_args()
    agent = ATETriageAgent()

    if args.file:
        if not os.path.isfile(args.file):
            print(f"Error: Log file not found at '{args.file}'", file=sys.stderr)
            sys.exit(1)
        with open(args.file, "r") as f:
            content = f.read()
        assessment = agent.triage_log(content)
        if args.json:
            print(json.dumps(assessment.dict(), indent=2))
        else:
            print_triage_card(assessment)
        sys.exit(0)

    print("\n[INFO] Running full 4-Scenario ATE Failure Demo...")
    scenarios = [
        ("SCENARIO 1: Socket Wear / Pogo Pin Degradation", SyntheticDataGenerator.generate_socket_wear_log(site=3)),
        ("SCENARIO 2: Thermal Excursion / Chiller Trip", SyntheticDataGenerator.generate_thermal_excursion_log(site=1)),
        ("SCENARIO 3: Deterministic Silicon Gate / Scan Fault", SyntheticDataGenerator.generate_silicon_gate_fault_log(site=2)),
        ("SCENARIO 4: High di/dt Transient VDD Supply Droop", SyntheticDataGenerator.generate_power_droop_log(site=4)),
    ]
    for title, log_text in scenarios:
        print(f"\n>>> Executing {title}")
        report = agent.triage_log(log_text)
        print_triage_card(report)


if __name__ == "__main__":
    main()
