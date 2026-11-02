"""
Unit tests for CMS Section 111 CMP Gap-Audit & Mitigation Engine
"""

import os
import json
import csv
import unittest
from datetime import date
from reconciliation_engine import (
    ClaimRecord,
    OTCRResponseRecord,
    Section111Auditor,
    AuditPacketGenerator,
    CMP_DAILY_RATES_2026,
    MAX_STATUTORY_PENALTY_PER_RECORD,
    SAMPLE_SELF_INSURED_RRES
)

class TestSection111Auditor(unittest.TestCase):
    def test_compliant_within_one_year(self):
        # Trigger 2024-01-15, deadline 2025-01-15, reported 2024-12-01 -> Compliant
        result = Section111Auditor.calculate_delinquency(
            trigger_date_str="2024-01-15",
            reported_date_str="2024-12-01",
            as_of_date=date(2026, 11, 2)
        )
        self.assertEqual(result["days_delinquent"], 0)
        self.assertEqual(result["status"], "COMPLIANT_ACCEPTED")
        self.assertEqual(result["penalty"], 0.0)

    def test_tier_1_delinquency(self):
        # Trigger 2024-06-01, deadline 2025-06-01. As of 2026-06-01 (365 days late) -> Tier 1 ($378/day)
        result = Section111Auditor.calculate_delinquency(
            trigger_date_str="2024-06-01",
            reported_date_str=None,
            as_of_date=date(2026, 6, 1)
        )
        self.assertEqual(result["status"], "NON_COMPLIANT_DELINQUENT")
        self.assertEqual(result["tier"], "TIER_1")
        self.assertEqual(result["daily_rate"], 378.0)
        self.assertAlmostEqual(result["penalty"], 365 * 378.0)

    def test_tier_2_delinquency(self):
        # Trigger 2023-01-01, deadline 2024-01-01. As of 2026-02-01 (762 days late) -> Tier 2 ($756/day, capped at $551,880)
        result = Section111Auditor.calculate_delinquency(
            trigger_date_str="2023-01-01",
            reported_date_str=None,
            as_of_date=date(2026, 2, 1)
        )
        self.assertEqual(result["status"], "NON_COMPLIANT_DELINQUENT")
        self.assertEqual(result["tier"], "TIER_2")
        self.assertEqual(result["daily_rate"], 756.0)
        self.assertEqual(result["penalty"], MAX_STATUTORY_PENALTY_PER_RECORD)

    def test_statutory_maximum_cap(self):
        # More than 1000 days late at $1,512/day would exceed $551,880 -> capped
        result = Section111Auditor.calculate_delinquency(
            trigger_date_str="2020-01-01",
            reported_date_str=None,
            as_of_date=date(2026, 11, 2)
        )
        self.assertEqual(result["tier"], "TIER_3")
        self.assertEqual(result["penalty"], MAX_STATUTORY_PENALTY_PER_RECORD)

    def test_audit_claims_reconciliation(self):
        claims = [
            ClaimRecord(
                claim_id="TEST-001",
                rre_id="RRE-TEST",
                injured_party_name="Alice Smith",
                mbi_or_ssn="1AA2-BB3-CC44",
                date_of_incident="2023-01-01",
                tpoc_date="2023-05-01",
                tpoc_amount=50000.0,
                orm_indicator="N",
                orm_termination_date=None,
                claim_type="General Liability",
                claim_adjuster="Adjuster 1"
            )
        ]
        # Never reported
        findings = Section111Auditor.audit_claims(claims, [], as_of_date=date(2026, 11, 2))
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].claim_id, "TEST-001")
        self.assertEqual(findings[0].status, "NON_COMPLIANT_DELINQUENT")
        self.assertGreater(findings[0].estimated_cmp_exposure, 100000.0)

class TestAuditPacketGenerator(unittest.TestCase):
    def setUp(self):
        self.test_dir = "/tmp/cms_audit_test"
        os.makedirs(self.test_dir, exist_ok=True)
        self.csv_path = os.path.join(self.test_dir, "test_findings.csv")

    def tearDown(self):
        if os.path.exists(self.csv_path):
            os.remove(self.csv_path)

    def test_export_audit_csv(self):
        claims = [
            ClaimRecord(
                claim_id="TEST-CSV",
                rre_id="RRE-TEST",
                injured_party_name="Bob Jones",
                mbi_or_ssn="9ZZ9-YY8-XX77",
                date_of_incident="2023-01-01",
                tpoc_date="2023-06-01",
                tpoc_amount=25000.0,
                orm_indicator="N",
                orm_termination_date=None,
                claim_type="Commercial Auto",
                claim_adjuster="Adjuster 2"
            )
        ]
        findings = Section111Auditor.audit_claims(claims, [], as_of_date=date(2026, 11, 2))
        AuditPacketGenerator.export_audit_csv(findings, self.csv_path)

        self.assertTrue(os.path.exists(self.csv_path))
        with open(self.csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            rows = list(reader)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["claim_id"], "TEST-CSV")
            self.assertIn("estimated_cmp_exposure", reader.fieldnames)

    def test_generate_packet_markdown(self):
        claims = [
            ClaimRecord(
                claim_id="TEST-MD",
                rre_id="RRE-TEST",
                injured_party_name="Carol White",
                mbi_or_ssn="4KK4-JJ5-HH66",
                date_of_incident="2023-01-01",
                tpoc_date="2023-07-01",
                tpoc_amount=15000.0,
                orm_indicator="N",
                orm_termination_date=None,
                claim_type="Workers Comp",
                claim_adjuster="Adjuster 3"
            )
        ]
        findings = Section111Auditor.audit_claims(claims, [], as_of_date=date(2026, 11, 2))
        packet = AuditPacketGenerator.generate_audit_packet_markdown(findings, SAMPLE_SELF_INSURED_RRES)

        self.assertIn("CMS Section 111 (NGHP) CMP Audit", packet)
        self.assertIn("CMS SECTION 111 INFORMAL NOTICE MITIGATION RESPONSE", packet)
        self.assertIn("42 CFR § 402.1", packet)
        self.assertIn("Robert Ketler", packet)

if __name__ == "__main__":
    unittest.main()
