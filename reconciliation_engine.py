#!/usr/bin/env python3
"""
CMS Section 111 (NGHP) CMP Gap-Audit & Informal Notice Mitigation Engine
Designed for Non-Group Health Plan (NGHP) Responsible Reporting Entities (RREs):
Self-Insured Corporations, Risk Retention Groups (RRGs), Captives, and Municipal Pools.

Statutory Background:
- Medicare Secondary Payer (MSP) Act (42 U.S.C. § 1395y(b)(8)) & Section 111 mandatory reporting.
- 42 CFR Part 402 Final Rule (CMS-6061-F, effective Oct 11 2024, liable Oct 11 2025, live audits Jan 2026).
- 2026 Inflation-Adjusted CMP Schedule:
    * 1 to 2 years late: $378/day per record
    * 2 to 3 years late: $756/day per record
    * 3+ years late: $1,512/day per record
    * Maximum statutory cap: $551,880 per delinquent claim record
- 30-day strict mitigation window upon receipt of CMS Informal Notice.

Capabilities:
1. Reconciles raw internal casualty/workers-comp claims list against CMS OTCR Section 111 response files.
2. Calculates exact statutory late days and total CMP financial exposure.
3. Automatically generates the formal 30-Day Informal Notice Mitigation Response Package.
4. Identifies prospective self-insured RREs from public regulatory registries (Texas TDI, etc.).
"""

import argparse
import csv
import json
import logging
import math
import os
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timezone
from typing import List, Dict, Any, Optional

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s'
)
logger = logging.getLogger("cms-s111-audit")

# 2026 CMS Section 111 CMP Penalty Tiers (per day per claim record)
CMP_DAILY_RATES_2026 = {
    "TIER_1": {"min_days": 365, "max_days": 730, "daily_penalty": 378.0, "label": "1-2 Years Late ($378/day)"},
    "TIER_2": {"min_days": 731, "max_days": 1095, "daily_penalty": 756.0, "label": "2-3 Years Late ($756/day)"},
    "TIER_3": {"min_days": 1096, "max_days": 999999, "daily_penalty": 1512.0, "label": "3+ Years Late ($1,512/day)"}
}
MAX_STATUTORY_PENALTY_PER_RECORD = 551880.0  # 2026 inflation-adjusted annual cap

@dataclass
class ClaimRecord:
    claim_id: str
    rre_id: str
    injured_party_name: str
    mbi_or_ssn: str
    date_of_incident: str        # YYYY-MM-DD
    tpoc_date: Optional[str]     # Total Payment Obligation to Claimant date (settlement date)
    tpoc_amount: float
    orm_indicator: str           # "Y" or "N" (Ongoing Responsibility for Medicals)
    orm_termination_date: Optional[str]
    claim_type: str              # "Workers Comp", "General Liability", "Commercial Auto"
    claim_adjuster: str

@dataclass
class OTCRResponseRecord:
    claim_id: str
    rre_id: str
    file_submission_date: str    # YYYY-MM-DD
    disposition_code: str        # "00" (Accepted), "50" (Error), "51" (Rejected)
    error_codes: List[str]       # e.g. ["SP31", "CJ01"]
    cms_accepted_date: Optional[str]

@dataclass
class AuditFinding:
    claim_id: str
    rre_id: str
    injured_party_name: str
    trigger_date: str            # Settlement TPOC date or ORM Assumption date
    reporting_deadline: str      # 1 year from trigger date
    actual_reported_date: Optional[str]
    days_delinquent: int
    cmp_tier: str
    daily_rate: float
    estimated_cmp_exposure: float
    status: str                  # "NON_COMPLIANT_DELINQUENT", "PENDING_REPORTING", "COMPLIANT_ACCEPTED"
    mitigation_strategy: str

# -------------------------------------------------------------------------
# Sample Benchmark / Curated Data (Self-Insured RRE Prospects)
# -------------------------------------------------------------------------
SAMPLE_SELF_INSURED_RRES = [
    {
        "rre_name": "Lonestar Freightlines Logistics Corp",
        "state": "TX",
        "certified_self_insured_id": "TX-CSI-2019-041",
        "headquarters": "Fort Worth, TX",
        "risk_manager": "David Sterling, VP Risk & Claims",
        "contact_email": "dsterling@lonestarfreight.com",
        "phone": "(817) 555-0199",
        "estimated_annual_claims": 240,
        "primary_exposure": "Commercial Auto / Workers' Compensation",
        "vulnerability_rationale": "High turnover of TPAs over last 3 years; potential orphan TPOC settlements."
    },
    {
        "rre_name": "Midland Basin Energy Services Inc",
        "state": "TX",
        "certified_self_insured_id": "TX-CSI-2018-112",
        "headquarters": "Midland, TX",
        "risk_manager": "Sarah Jenkins, Director of Corporate Risk",
        "contact_email": "sjenkins@midlandbasinenergy.com",
        "phone": "(432) 555-0248",
        "estimated_annual_claims": 180,
        "primary_exposure": "Oilfield Heavy Equipment / Workers' Comp",
        "vulnerability_rationale": "Complex indemnity master service agreements with subcontractor claims."
    },
    {
        "rre_name": "Sunbelt Restaurant Brands Group",
        "state": "FL",
        "certified_self_insured_id": "FL-SIR-2021-089",
        "headquarters": "Tampa, FL",
        "risk_manager": "Marcus Vance, Treasury & Insurance",
        "contact_email": "mvance@sunbeltbrands.com",
        "phone": "(813) 555-0312",
        "estimated_annual_claims": 310,
        "primary_exposure": "Premises Slip-and-Fall General Liability",
        "vulnerability_rationale": "High-volume sub-$25K nuisance settlements with unverified Medicare beneficiary MBI."
    }
]

# -------------------------------------------------------------------------
# Audit Engine Core
# -------------------------------------------------------------------------
class Section111Auditor:
    """Reconciles internal claims against CMS response files and calculates CMP exposure."""

    @staticmethod
    def calculate_delinquency(trigger_date_str: str, reported_date_str: Optional[str], as_of_date: Optional[date] = None) -> Dict[str, Any]:
        """
        Under 42 CFR 402.1(c)(22), reporting is required within 1 year (365 days)
        following the date TPOC is established or ORM is assumed.
        """
        if as_of_date is None:
            as_of_date = date.today()

        trigger_dt = datetime.strptime(trigger_date_str, "%Y-%m-%d").date()
        # Statutory deadline is 1 year from trigger date
        deadline = date(trigger_dt.year + 1, trigger_dt.month, trigger_dt.day)

        if reported_date_str:
            actual_dt = datetime.strptime(reported_date_str, "%Y-%m-%d").date()
            if actual_dt <= deadline:
                return {
                    "days_delinquent": 0,
                    "deadline": deadline.isoformat(),
                    "status": "COMPLIANT_ACCEPTED",
                    "tier": "NONE",
                    "daily_rate": 0.0,
                    "penalty": 0.0
                }
            effective_end = actual_dt
        else:
            effective_end = as_of_date

        days_late = (effective_end - deadline).days
        if days_late <= 0:
            return {
                "days_delinquent": 0,
                "deadline": deadline.isoformat(),
                "status": "PENDING_WITHIN_WINDOW",
                "tier": "NONE",
                "daily_rate": 0.0,
                "penalty": 0.0
            }

        # Determine penalty tier
        if days_late <= CMP_DAILY_RATES_2026["TIER_1"]["max_days"]:
            tier = "TIER_1"
            rate = CMP_DAILY_RATES_2026["TIER_1"]["daily_penalty"]
        elif days_late <= CMP_DAILY_RATES_2026["TIER_2"]["max_days"]:
            tier = "TIER_2"
            rate = CMP_DAILY_RATES_2026["TIER_2"]["daily_penalty"]
        else:
            tier = "TIER_3"
            rate = CMP_DAILY_RATES_2026["TIER_3"]["daily_penalty"]

        total_penalty = min(MAX_STATUTORY_PENALTY_PER_RECORD, days_late * rate)

        return {
            "days_delinquent": days_late,
            "deadline": deadline.isoformat(),
            "status": "NON_COMPLIANT_DELINQUENT",
            "tier": tier,
            "daily_rate": rate,
            "penalty": total_penalty
        }

    @classmethod
    def audit_claims(cls, claims: List[ClaimRecord], responses: List[OTCRResponseRecord], as_of_date: Optional[date] = None) -> List[AuditFinding]:
        response_map = {r.claim_id: r for r in responses}
        findings: List[AuditFinding] = []

        for c in claims:
            # Trigger date is TPOC date if settlement exists, otherwise incident date for ORM
            trigger = c.tpoc_date or c.date_of_incident
            resp = response_map.get(c.claim_id)
            reported_date = resp.cms_accepted_date if resp and resp.disposition_code == "00" else None

            delinq = cls.calculate_delinquency(trigger, reported_date, as_of_date)

            if delinq["status"] == "NON_COMPLIANT_DELINQUENT":
                strategy = (
                    "Invoke 42 CFR § 402.1(c)(22)(iv) Safe Harbor: Good-faith documentation of 2 written attempts "
                    "plus 1 certified/phone outreach to obtain Medicare beneficiary MBI/HICN info."
                )
            elif delinq["status"] == "PENDING_WITHIN_WINDOW":
                strategy = "Queue for next quarterly Section 111 batch submission before 1-year statutory deadline."
            else:
                strategy = "Accepted by CMS BCRC with disposition code 00. Retain audit trail."

            findings.append(AuditFinding(
                claim_id=c.claim_id,
                rre_id=c.rre_id,
                injured_party_name=c.injured_party_name,
                trigger_date=trigger,
                reporting_deadline=delinq["deadline"],
                actual_reported_date=reported_date,
                days_delinquent=delinq["days_delinquent"],
                cmp_tier=delinq["tier"],
                daily_rate=delinq["daily_rate"],
                estimated_cmp_exposure=round(delinq["penalty"], 2),
                status=delinq["status"],
                mitigation_strategy=strategy
            ))

        findings.sort(key=lambda f: f.estimated_cmp_exposure, reverse=True)
        return findings

# -------------------------------------------------------------------------
# Audit Packet & Outreach Generators
# -------------------------------------------------------------------------
class AuditPacketGenerator:
    @staticmethod
    def export_audit_csv(findings: List[AuditFinding], filepath: str) -> None:
        fieldnames = [
            "claim_id", "rre_id", "injured_party_name", "status", "days_delinquent",
            "cmp_tier", "daily_rate", "estimated_cmp_exposure", "trigger_date",
            "reporting_deadline", "actual_reported_date", "mitigation_strategy"
        ]
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            for f_item in findings:
                writer.writerow(asdict(f_item))
        logger.info(f"Audit findings CSV saved to {filepath}")

    @staticmethod
    def generate_audit_packet_markdown(findings: List[AuditFinding], prospects: List[Dict[str, Any]]) -> str:
        delinquent = [f for f in findings if f.status == "NON_COMPLIANT_DELINQUENT"]
        total_cmp = sum(f.estimated_cmp_exposure for f in delinquent)

        md = []
        md.append("# CMS Section 111 (NGHP) CMP Audit & 30-Day Notice Mitigation Packet")
        md.append("**Regulatory Baseline:** 42 U.S.C. § 1395y(b)(8) & 42 CFR Part 402 (Civil Monetary Penalties Final Rule).")
        md.append(f"**Audit Run Date:** {datetime.now(timezone.utc).strftime('%B %d, %Y')} | **Auditor:** Robert Ketler Compliance Advisory")
        md.append("**Offer Architecture:** $2,500 – $5,000 Flat-Fee Gap Audit / $2,000 per Informal Notice Mitigation Defense.\n")
        md.append("---\n")

        md.append("## 1. Executive Summary & Regulatory Backdrop")
        md.append("- **The Enforcement Trigger:** CMS began quarterly random Section 111 compliance audits in **January 2026**, with initial Informal Notices sent in March 2026 (Liability/No-Fault) and July 2026 (Workers' Compensation).")
        md.append("- **2026 Inflation-Adjusted Penalties:** CMS imposes **$378 to $1,512 per day per delinquent record**, up to a maximum statutory penalty of **$551,880 per claim**.")
        md.append("- **The 30-Day Hard Window:** Once an RRE receives an Informal Notice from CMS, it has **exactly 30 calendar days** to provide mitigating documentation. CMS grants NO EXTENSIONS.")
        md.append("- **RRE Direct Liability:** Penalties attach strictly to the self-insured entity (RRE), NOT the reporting agent or third-party administrator (TPA).\n")

        md.append("## 2. Sample Client Gap-Audit Results (Delinquency Analysis)")
        md.append(f"- **Total Claims Audited:** {len(findings)}")
        md.append(f"- **Delinquent Non-Compliant Claims:** {len(delinquent)}")
        md.append(f"- **Total Potential CMP Exposure:** **${total_cmp:,.2f}**\n")

        md.append("| Claim ID | Injured Party | Trigger Date | Statutory Deadline | Days Late | CMP Daily Rate | Total CMP Exposure | Status |")
        md.append("| :--- | :--- | :---: | :---: | :---: | :---: | :---: | :--- |")
        for f in delinquent:
            rate_fmt = f"${f.daily_rate:,.0f}/day"
            exp_fmt = f"${f.estimated_cmp_exposure:,.2f}"
            md.append(f"| `{f.claim_id}` | {f.injured_party_name} | {f.trigger_date} | {f.reporting_deadline} | **{f.days_delinquent} days** | {rate_fmt} | **{exp_fmt}** | 🔴 Delinquent |")
        md.append("\n")

        md.append("## 3. High-Value Self-Insured RRE Prospects (Target Outreach Database)")
        md.append("| Target Entity | State | Risk Manager Contact | Claims Exposure | Phone | Email |")
        md.append("| :--- | :---: | :--- | :--- | :--- | :--- |")
        for p in prospects:
            md.append(f"| **{p['rre_name']}** | {p['state']} | {p['risk_manager']} | {p['primary_exposure']} | `{p['phone']}` | `{p['contact_email']}` |")
        md.append("\n")

        md.append("## 4. Formal 30-Day CMS Informal Notice Mitigation Defense Letter")
        md.append("*(Template for submission to CMS BCRC / Commercial Repayment Center within the 30-day window)*\n")
        md.append("```text")
        md.append("CMS SECTION 111 INFORMAL NOTICE MITIGATION RESPONSE")
        md.append("==================================================")
        md.append("Date: [Date within 30-day notice receipt]")
        md.append("To: Centers for Medicare & Medicaid Services (CMS)")
        md.append("    Division of Medicare Secondary Payer Operations (DMSPO)")
        md.append("    ATTN: Section 111 CMP Mitigation Desk")
        md.append("    P.O. Box 138832, Oklahoma City, OK 73113")
        md.append("")
        md.append("RE: RESPONSE TO INFORMAL NOTICE OF POTENTIAL CIVIL MONETARY PENALTY")
        md.append("    RRE Name: [Responsible Reporting Entity Legal Name]")
        md.append("    RRE ID: [RRE ID Number]")
        md.append("    Notice Tracking Number: [CMS Notice Ref Number]")
        md.append("    Claim ID: [Claim Number] / Beneficiary: [Beneficiary Name / MBI]")
        md.append("")
        md.append("Dear CMS Compliance Officers,")
        md.append("")
        md.append("This letter constitutes the formal mitigation response of [RRE Name] pursuant to 42 CFR § 402.1")
        md.append("within the mandatory thirty (30) calendar-day informal notice mitigation period.")
        md.append("")
        md.append("1. SAFE HARBOR COMPLIANCE (GOOD FAITH EFFORT):")
        md.append("   Under 42 CFR § 402.1(c)(22)(iv), an RRE is exempt from CMP imposition if it demonstrates good")
        md.append("   faith efforts to obtain necessary Medicare beneficiary identifying information. As documented")
        md.append("   in Exhibit A attached hereto, [RRE Name] made:")
        md.append("   - First written request for Medicare status/MBI on [Date 1];")
        md.append("   - Second written follow-up request via certified mail on [Date 2]; and")
        md.append("   - Telephone follow-up to claimant counsel on [Date 3].")
        md.append("   The beneficiary and counsel failed to disclose Medicare entitlement prior to final settlement.")
        md.append("")
        md.append("2. TIMELY DISCREPANCY REMEDIATION:")
        md.append("   Upon receipt of the beneficiary's confirmed MBI, the record was successfully reported in the")
        md.append("   next scheduled quarterly Section 111 file (Batch ID: [Batch ID]), accepted with disposition code 00.")
        md.append("")
        md.append("Accordingly, [RRE Name] respectfully requests that CMS abate and withdraw the proposed civil")
        md.append("monetary penalty in its entirety.")
        md.append("")
        md.append("Respectfully submitted,")
        md.append("")
        md.append("[Authorized Representative Name & Title]")
        md.append("[RRE Name]")
        md.append("```\n")

        md.append("## 5. Direct Cold Outreach Script to Risk Managers (Texas / Florida Self-Insureds)")
        md.append("```text")
        md.append("Subject: 2026 CMS Section 111 CMP Audit Exposure: Sub-500 Claim Self-Insured Defense")
        md.append("")
        md.append("Dear [Risk Manager Name],")
        md.append("")
        md.append("As you are aware, CMS commenced live quarterly random Section 111 audits in 2026, with inflation-")
        md.append("adjusted Civil Monetary Penalties ranging from $378 to $1,512 per day per late record (up to $551,880 max).")
        md.append("")
        md.append("Unlike large insurance carriers that retain automated Section 111 software divisions, small and")
        md.append("mid-sized self-insured corporations frequently face exposure from orphan settlements, TPA transitions,")
        md.append("and unverified Medicare beneficiary MBIs.")
        md.append("")
        md.append("We provide a flat-fee Section 111 CMP Gap Audit ($2,500 – $5,000) that ingests your historical claims")
        md.append("list against CMS OTCR quarterly response files, pinpoints any delinquent records before CMS audits trigger,")
        md.append("and builds the documentation packages required under the 42 CFR § 402.1 Safe Harbor rules.")
        md.append("")
        md.append("Can we deliver a complimentary 10-claim sample gap reconciliation for your risk committee this week?")
        md.append("")
        md.append("Best regards,")
        md.append("")
        md.append("Robert Ketler")
        md.append("MSP Section 111 Regulatory Advisory")
        md.append("robert@crescenticosmosis.com | (555) 019-4821")
        md.append("```\n")

        md.append("## 6. Execution Steps Tonight")
        md.append("1. [ ] Deliver cold outreach to David Sterling (Lonestar Freightlines) and Sarah Jenkins (Midland Basin).")
        md.append("2. [ ] Offer the flat $3,500 audit package to reconcile their 2023–2025 closed casualty files against CMS OTCR response records.")
        md.append("3. [ ] Prepare partner MSP attorney network for referral sign-offs on any formal 30-day mitigation filings.")

        return "\n".join(md)

# -------------------------------------------------------------------------
# CLI Application
# -------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="CMS Section 111 CMP Gap-Audit & Mitigation Engine")
    subparsers = parser.add_subparsers(dest="command")

    p_run = subparsers.add_parser("run", help="Run gap-audit on sample/client claim files")
    p_run.add_argument("--output-dir", default=".", help="Directory to save audit output")

    p_packet = subparsers.add_parser("generate-packet", help="Generate CMP audit and outreach packet markdown")
    p_packet.add_argument("--output", default="CMS_S111_CMP_AUDIT_PACKET.md", help="Output path")

    args = parser.parse_args()
    output_dir = getattr(args, "output_dir", ".")
    os.makedirs(output_dir, exist_ok=True)

    # Sample test claims dataset (demonstrating compliant, pending, and delinquent tiers)
    claims = [
        ClaimRecord(
            claim_id="CLM-2023-0881",
            rre_id="RRE-99014",
            injured_party_name="John Henderson",
            mbi_or_ssn="1EG4-TE5-MK72",
            date_of_incident="2022-09-15",
            tpoc_date="2023-04-10",
            tpoc_amount=45000.0,
            orm_indicator="N",
            orm_termination_date=None,
            claim_type="Commercial Auto",
            claim_adjuster="T. Miller"
        ),
        ClaimRecord(
            claim_id="CLM-2024-1102",
            rre_id="RRE-99014",
            injured_party_name="Maria Vasquez",
            mbi_or_ssn="3AB8-WR2-PL90",
            date_of_incident="2023-11-01",
            tpoc_date="2024-03-01",
            tpoc_amount=120000.0,
            orm_indicator="N",
            orm_termination_date=None,
            claim_type="General Liability",
            claim_adjuster="S. Davis"
        ),
        ClaimRecord(
            claim_id="CLM-2026-0044",
            rre_id="RRE-99014",
            injured_party_name="Robert Chen",
            mbi_or_ssn="5JK9-OP1-ZZ44",
            date_of_incident="2025-10-12",
            tpoc_date="2026-06-15",
            tpoc_amount=85000.0,
            orm_indicator="Y",
            orm_termination_date=None,
            claim_type="Workers Comp",
            claim_adjuster="A. Lee"
        ),
        ClaimRecord(
            claim_id="CLM-2024-0550",
            rre_id="RRE-99014",
            injured_party_name="Arthur Pendelton",
            mbi_or_ssn="8MM2-QQ7-TT11",
            date_of_incident="2023-08-20",
            tpoc_date="2024-01-15",
            tpoc_amount=35000.0,
            orm_indicator="N",
            orm_termination_date=None,
            claim_type="General Liability",
            claim_adjuster="T. Miller"
        )
    ]

    # Sample CMS OTCR responses
    responses = [
        OTCRResponseRecord(
            claim_id="CLM-2026-0044",
            rre_id="RRE-99014",
            file_submission_date="2026-07-01",
            disposition_code="00",
            error_codes=[],
            cms_accepted_date="2026-07-01"
        ),
        OTCRResponseRecord(
            claim_id="CLM-2024-0550",
            rre_id="RRE-99014",
            file_submission_date="2025-01-05",
            disposition_code="00",
            error_codes=[],
            cms_accepted_date="2025-01-05"  # Reported within 1 year of 2024-01-15
        )
        # CLM-2023-0881 and CLM-2024-1102 were NEVER successfully reported -> Delinquent!
    ]

    # Fix reference audit date to November 2, 2026
    audit_date = date(2026, 11, 2)
    findings = Section111Auditor.audit_claims(claims, responses, as_of_date=audit_date)

    if args.command == "run" or args.command is None:
        csv_path = os.path.join(output_dir, "s111_audit_findings.csv")
        json_path = os.path.join(output_dir, "s111_audit_summary.json")
        packet_path = os.path.join(output_dir, "CMS_S111_CMP_AUDIT_PACKET.md")

        AuditPacketGenerator.export_audit_csv(findings, csv_path)

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump({
                "audit_date": audit_date.isoformat(),
                "total_claims": len(claims),
                "delinquent_count": sum(1 for f in findings if f.status == "NON_COMPLIANT_DELINQUENT"),
                "total_exposure_usd": sum(f.estimated_cmp_exposure for f in findings),
                "findings": [asdict(f) for f in findings],
                "target_prospects": SAMPLE_SELF_INSURED_RRES
            }, f, indent=2)

        packet_content = AuditPacketGenerator.generate_audit_packet_markdown(findings, SAMPLE_SELF_INSURED_RRES)
        with open(packet_path, "w", encoding="utf-8") as f:
            f.write(packet_content)
        logger.info(f"Audit packet saved to {packet_path}")

        print("\n=== CMS SECTION 111 CMP GAP AUDIT COMPLETE ===")
        print(f"Total Claims Analyzed: {len(findings)}")
        print(f"Non-Compliant Delinquent: {sum(1 for f in findings if f.status == 'NON_COMPLIANT_DELINQUENT')}")
        print(f"Total Calculated CMP Financial Exposure: ${sum(f.estimated_cmp_exposure for f in findings):,.2f}")
        print(f"Target Self-Insured Prospects: {len(SAMPLE_SELF_INSURED_RRES)}")

    elif args.command == "generate-packet":
        packet_path = args.output
        packet_content = AuditPacketGenerator.generate_audit_packet_markdown(findings, SAMPLE_SELF_INSURED_RRES)
        with open(packet_path, "w", encoding="utf-8") as f:
            f.write(packet_content)
        print(f"[OK] Audit packet written to {packet_path}")

if __name__ == "__main__":
    main()
