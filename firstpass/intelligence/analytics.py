"""
analytics.py — Pattern detection, trend analysis, and autonomous reasoning.

Mines accumulated data (work events, agent feedback, analytics snapshots,
corporate memory, message bus) to detect patterns, predict issues, track
decision outcomes, and generate proactive recommendations.  This is the
intelligence layer that makes the FirstPass EDI system grow smarter over time.

Source lineage: intelligence_engine.py (ceo-bot)

Usage::

    from firstpass.memory.corporate_memory import CorporateMemory
    from firstpass.utils.message_bus import MessageBus
    from firstpass.intelligence.analytics import IntelligenceEngine

    memory = CorporateMemory()
    bus = MessageBus()
    engine = IntelligenceEngine(memory, bus)

    briefing = engine.generate_briefing()
    patterns = engine.detect_patterns()
    predictions = engine.predict_today()
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("firstpass.intelligence.analytics")

_ROOT = Path(__file__).parent.parent.parent  # firstpass-edi/

STORE_DIR = _ROOT / "knowledge_store"
JOURNAL_FILE = STORE_DIR / "decision_journal.json"
PATTERNS_FILE = STORE_DIR / "detected_patterns.json"
REPORTS_DIR = STORE_DIR / "intel_reports"

WORK_LOG_FILE = _ROOT / "logs" / "work_log.json"
ANALYTICS_FILE = _ROOT / "logs" / "analytics.json"
FEEDBACK_FILE = _ROOT / "logs" / "feedback.json"

DAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday",
    "friday", "saturday", "sunday",
]


class IntelligenceEngine:
    """Pattern detection, trend analysis, and autonomous reasoning for FirstPass EDI."""

    def __init__(self, memory, bus=None):
        """
        :param memory: :class:`~firstpass.memory.corporate_memory.CorporateMemory` instance.
        :param bus:    Optional :class:`~firstpass.utils.message_bus.MessageBus` instance.
        """
        self.memory = memory
        self.bus = bus
        self._decision_journal: List[Dict[str, Any]] = self._load_journal()
        self._patterns: List[Dict[str, Any]] = self._load_patterns()
        self._counter = len(self._decision_journal)

        STORE_DIR.mkdir(parents=True, exist_ok=True)
        REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Pattern Detection
    # ------------------------------------------------------------------

    def detect_patterns(self) -> List[Dict[str, Any]]:
        """Mine work events, feedback, and analytics for recurring patterns.

        Combines temporal / frequency analysis with optional LLM-assisted
        insight extraction.

        :returns: List of new or updated pattern dicts.
        """
        work_log = self._load_work_log()
        if not work_log:
            return []

        new_patterns: List[Dict] = []
        new_patterns.extend(self._detect_temporal_patterns(work_log))
        new_patterns.extend(self._detect_error_correlations(work_log))
        new_patterns.extend(self._detect_performance_trends())
        new_patterns.extend(self._detect_delegation_patterns(work_log))

        for pattern in new_patterns:
            existing = self._find_existing_pattern(pattern["description"])
            if existing:
                existing["confidence"] = min(0.99, existing["confidence"] + 0.05)
                existing["last_updated"] = datetime.now().isoformat()
                existing["evidence"].extend(pattern.get("evidence", []))
                existing["evidence"] = existing["evidence"][-20:]
            else:
                pattern["id"] = f"pat_{len(self._patterns) + 1:04d}"
                pattern["first_detected"] = datetime.now().isoformat()
                pattern["last_updated"] = datetime.now().isoformat()
                self._patterns.append(pattern)

            # Also store in corporate memory
            self.memory.remember(
                category="pattern",
                topic=pattern.get("description", "unknown_pattern")[:60]
                .replace(" ", "_")
                .lower(),
                content=pattern["description"],
                source="IntelligenceEngine",
                confidence=pattern.get("confidence", 0.5),
                tags=pattern.get("tags", []),
            )

        self._save_patterns()
        logger.info(f"[analytics] Detected {len(new_patterns)} patterns")
        return new_patterns

    def _detect_temporal_patterns(self, work_log: List[Dict]) -> List[Dict]:
        """Group events by day-of-week and find anomalies."""
        patterns: List[Dict] = []
        day_agent_counts: Dict = defaultdict(lambda: defaultdict(int))
        day_error_counts: Dict = defaultdict(lambda: defaultdict(int))

        for entry in work_log:
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                day = DAY_NAMES[ts.weekday()]
                agent = entry.get("agent") or entry.get("bot") or entry.get("bot_name", "unknown")
                day_agent_counts[day][agent] += 1
                if entry.get("status") in ("failed", "error"):
                    day_error_counts[day][agent] += 1
            except Exception:
                continue

        if not day_agent_counts:
            return patterns

        for day in DAY_NAMES:
            for agent, error_count in day_error_counts[day].items():
                total_for_agent = sum(day_agent_counts[d][agent] for d in DAY_NAMES)
                if total_for_agent == 0:
                    continue
                avg_errors_per_day = (
                    sum(day_error_counts[d][agent] for d in DAY_NAMES) / 7
                )
                if avg_errors_per_day > 0 and error_count > avg_errors_per_day * 2:
                    patterns.append(
                        {
                            "type": "temporal",
                            "description": (
                                f"{agent} has {error_count} errors on {day.title()}s "
                                f"(avg {avg_errors_per_day:.1f}/day)"
                            ),
                            "confidence": min(
                                0.9, 0.5 + (error_count / total_for_agent)
                            ),
                            "evidence": [
                                {
                                    "day": day,
                                    "error_count": error_count,
                                    "avg": round(avg_errors_per_day, 1),
                                }
                            ],
                            "actionable": True,
                            "suggested_action": f"Pre-check {agent} on {day.title()} mornings",
                            "tags": [agent.lower(), day, "temporal", "errors"],
                        }
                    )

        return patterns

    def _detect_error_correlations(self, work_log: List[Dict]) -> List[Dict]:
        """Find errors that co-occur within 1-hour windows."""
        patterns: List[Dict] = []
        errors = [e for e in work_log if e.get("status") in ("failed", "error")]

        if len(errors) < 3:
            return patterns

        hourly_groups: Dict = defaultdict(list)
        for error in errors:
            try:
                ts = datetime.fromisoformat(error["timestamp"])
                hour_key = ts.strftime("%Y-%m-%d_%H")
                hourly_groups[hour_key].append(error)
            except Exception:
                continue

        co_occurrence: Counter = Counter()
        for hour_key, group in hourly_groups.items():
            if len(group) < 2:
                continue
            agents = sorted(
                {e.get("agent") or e.get("bot") or e.get("bot_name", "unknown")
                 for e in group}
            )
            for i in range(len(agents)):
                for j in range(i + 1, len(agents)):
                    co_occurrence[(agents[i], agents[j])] += 1

        for (agent_a, agent_b), count in co_occurrence.most_common(5):
            if count >= 2:
                patterns.append(
                    {
                        "type": "correlation",
                        "description": (
                            f"{agent_a} and {agent_b} errors co-occur {count} times "
                            f"within the same hour"
                        ),
                        "confidence": min(0.85, 0.4 + count * 0.1),
                        "evidence": [{"agent_a": agent_a, "agent_b": agent_b, "count": count}],
                        "actionable": True,
                        "suggested_action": (
                            f"Investigate shared dependency between {agent_a} and {agent_b}"
                        ),
                        "tags": [agent_a.lower(), agent_b.lower(), "correlation", "errors"],
                    }
                )

        return patterns

    def _detect_performance_trends(self) -> List[Dict]:
        """Analyse the analytics snapshot for improving or declining agents."""
        patterns: List[Dict] = []
        try:
            if ANALYTICS_FILE.exists():
                analytics = json.loads(ANALYTICS_FILE.read_text(encoding="utf-8"))
            else:
                return patterns
        except Exception:
            return patterns

        for agent_name, stats in analytics.items():
            total = stats.get("success", 0) + stats.get("failure", 0)
            if total < 5:
                continue
            success_rate = stats["success"] / total * 100
            if success_rate < 70:
                patterns.append(
                    {
                        "type": "performance",
                        "description": (
                            f"{agent_name} has low success rate: {success_rate:.0f}% "
                            f"({stats['failure']} failures out of {total} runs)"
                        ),
                        "confidence": min(0.9, 0.5 + (total / 100)),
                        "evidence": [
                            {
                                "agent": agent_name,
                                "success_rate": round(success_rate, 1),
                                "total_runs": total,
                            }
                        ],
                        "actionable": True,
                        "suggested_action": (
                            f"Review {agent_name} error logs and adjust configuration"
                        ),
                        "tags": [agent_name.lower(), "performance", "declining"],
                    }
                )
            elif success_rate > 95 and total > 20:
                patterns.append(
                    {
                        "type": "performance",
                        "description": (
                            f"{agent_name} performing excellently: {success_rate:.0f}% "
                            f"success rate over {total} runs"
                        ),
                        "confidence": 0.9,
                        "evidence": [
                            {
                                "agent": agent_name,
                                "success_rate": round(success_rate, 1),
                                "total_runs": total,
                            }
                        ],
                        "actionable": False,
                        "suggested_action": None,
                        "tags": [agent_name.lower(), "performance", "excellent"],
                    }
                )

        return patterns

    def _detect_delegation_patterns(self, work_log: List[Dict]) -> List[Dict]:
        """Find which agents handle the highest share of tasks."""
        patterns: List[Dict] = []
        agent_counts: Counter = Counter()

        for entry in work_log:
            agent = entry.get("agent") or entry.get("bot") or entry.get("bot_name", "")
            if agent:
                agent_counts[agent] += 1

        total = sum(agent_counts.values())
        if total < 10:
            return patterns

        for agent, count in agent_counts.most_common():
            pct = count / total * 100
            if pct > 30:
                patterns.append(
                    {
                        "type": "delegation",
                        "description": f"{agent} handles {pct:.0f}% of all tasks ({count}/{total})",
                        "confidence": 0.8,
                        "evidence": [
                            {"agent": agent, "count": count, "pct": round(pct, 1)}
                        ],
                        "actionable": pct > 50,
                        "suggested_action": (
                            f"Consider load balancing — {agent} may be overloaded"
                            if pct > 50
                            else None
                        ),
                        "tags": [agent.lower(), "delegation", "workload"],
                    }
                )

        return patterns

    def _find_existing_pattern(self, description: str) -> Optional[Dict]:
        desc_lower = description.lower()
        for pattern in self._patterns:
            existing_lower = pattern.get("description", "").lower()
            desc_words = set(desc_lower.split())
            existing_words = set(existing_lower.split())
            overlap = len(desc_words & existing_words) / max(
                len(desc_words | existing_words), 1
            )
            if overlap > 0.6:
                return pattern
        return None

    # ------------------------------------------------------------------
    # Trend Analysis
    # ------------------------------------------------------------------

    def analyze_trends(self, window: str = "weekly") -> Dict[str, Any]:
        """Analyse metrics over time windows.

        :param window: ``"daily"`` (7 days) | ``"weekly"`` (28 days) | ``"monthly"`` (90 days).
        """
        work_log = self._load_work_log()
        feedback = self._load_feedback()

        days = {"daily": 7, "monthly": 90}.get(window, 28)
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        recent_work = [e for e in work_log if e.get("timestamp", "") > cutoff]
        recent_feedback = [f for f in feedback if f.get("timestamp", "") > cutoff]

        agent_trends: Dict = defaultdict(lambda: {"success": 0, "failure": 0, "total": 0})
        for entry in recent_work:
            agent = entry.get("agent") or entry.get("bot") or entry.get("bot_name", "unknown")
            agent_trends[agent]["total"] += 1
            if entry.get("status") in ("failed", "error"):
                agent_trends[agent]["failure"] += 1
            else:
                agent_trends[agent]["success"] += 1

        avg_rating = 0.0
        if recent_feedback:
            avg_rating = (
                sum(f.get("rating", 3) for f in recent_feedback) / len(recent_feedback)
            )

        daily_volume: Counter = Counter()
        for entry in recent_work:
            try:
                day = datetime.fromisoformat(entry["timestamp"]).strftime("%Y-%m-%d")
                daily_volume[day] += 1
            except Exception:
                continue

        volumes = list(daily_volume.values()) if daily_volume else [0]
        avg_daily = sum(volumes) / max(len(volumes), 1)

        return {
            "window": window,
            "period_days": days,
            "total_events": len(recent_work),
            "avg_daily_volume": round(avg_daily, 1),
            "avg_feedback_rating": round(avg_rating, 2),
            "agent_performance": {
                agent: {
                    "success_rate": round(
                        stats["success"] / max(stats["total"], 1) * 100, 1
                    ),
                    "total": stats["total"],
                }
                for agent, stats in agent_trends.items()
            },
            "notable_changes": self._find_notable_changes(recent_work),
        }

    def _find_notable_changes(self, recent_work: List[Dict]) -> List[str]:
        changes: List[str] = []
        if len(recent_work) < 10:
            return ["Insufficient data for trend detection"]

        mid = len(recent_work) // 2
        first_half = recent_work[:mid]
        second_half = recent_work[mid:]

        first_errors = sum(
            1 for e in first_half if e.get("status") in ("failed", "error")
        )
        second_errors = sum(
            1 for e in second_half if e.get("status") in ("failed", "error")
        )
        first_rate = first_errors / max(len(first_half), 1)
        second_rate = second_errors / max(len(second_half), 1)

        if second_rate > first_rate * 1.5 and second_errors > 2:
            changes.append(
                f"Error rate increasing: {first_rate*100:.0f}% -> {second_rate*100:.0f}%"
            )
        elif first_rate > second_rate * 1.5 and first_errors > 2:
            changes.append(
                f"Error rate improving: {first_rate*100:.0f}% -> {second_rate*100:.0f}%"
            )

        first_agents: Counter = Counter(
            e.get("agent") or e.get("bot") or e.get("bot_name", "") for e in first_half
        )
        second_agents: Counter = Counter(
            e.get("agent") or e.get("bot") or e.get("bot_name", "") for e in second_half
        )
        for agent in set(list(first_agents) + list(second_agents)):
            if agent and second_agents[agent] > first_agents[agent] * 2:
                changes.append(f"{agent} usage doubled recently")

        return changes if changes else ["No notable changes detected"]

    # ------------------------------------------------------------------
    # Predictions
    # ------------------------------------------------------------------

    def predict_today(self) -> List[Dict[str, Any]]:
        """Generate predictions for today based on accumulated patterns."""
        predictions: List[Dict] = []
        today = DAY_NAMES[datetime.now().weekday()]

        for pattern in self._patterns:
            if pattern.get("type") != "temporal":
                continue
            desc = pattern.get("description", "").lower()
            if today in desc:
                predictions.append(
                    {
                        "prediction": pattern["description"],
                        "confidence": pattern.get("confidence", 0.5),
                        "suggested_action": pattern.get("suggested_action"),
                        "source": f"Pattern {pattern.get('id', '?')}",
                    }
                )

        work_log = self._load_work_log()
        today_day = datetime.now().weekday()
        day_dates: List[str] = []
        for entry in work_log:
            try:
                ts = datetime.fromisoformat(entry["timestamp"])
                if ts.weekday() == today_day:
                    day_dates.append(ts.strftime("%Y-%m-%d"))
            except Exception:
                continue

        day_counts: Counter = Counter(day_dates)
        if day_counts:
            avg_volume = sum(day_counts.values()) / max(len(day_counts), 1)
            predictions.append(
                {
                    "prediction": (
                        f"Expected ~{avg_volume:.0f} agent activities today "
                        f"(based on {len(day_counts)} previous {today.title()}s)"
                    ),
                    "confidence": min(0.8, 0.4 + len(day_counts) * 0.05),
                    "suggested_action": None,
                    "source": "Historical volume",
                }
            )

        recent_decisions = [
            d for d in self._decision_journal if d.get("success") is not None
        ]
        if recent_decisions:
            success_rate = (
                sum(1 for d in recent_decisions if d.get("success"))
                / len(recent_decisions)
                * 100
            )
            predictions.append(
                {
                    "prediction": (
                        f"Decision success rate: {success_rate:.0f}% "
                        f"(based on {len(recent_decisions)} recorded decisions)"
                    ),
                    "confidence": 0.7,
                    "suggested_action": (
                        None if success_rate > 80 else "Review delegation strategy"
                    ),
                    "source": "Decision journal",
                }
            )

        return predictions

    # ------------------------------------------------------------------
    # Decision Journal
    # ------------------------------------------------------------------

    def record_decision(
        self,
        context: str,
        decision: str,
        delegated_to: str,
        expected_outcome: str = "",
    ) -> str:
        """Record a decision before it is acted upon.  Returns decision ID."""
        self._counter += 1
        dec_id = f"dec_{datetime.now().strftime('%Y%m%d')}_{self._counter:04d}"

        entry: Dict[str, Any] = {
            "id": dec_id,
            "timestamp": datetime.now().isoformat(),
            "context": context[:500],
            "decision": decision[:500],
            "delegated_to": delegated_to,
            "expected_outcome": expected_outcome[:200],
            "actual_outcome": None,
            "outcome_recorded_at": None,
            "success": None,
            "learnings": None,
        }

        self._decision_journal.append(entry)
        self._save_journal()
        return dec_id

    def record_outcome(
        self,
        decision_id: str,
        actual_outcome: str,
        success: bool,
        learnings: str = "",
    ) -> None:
        """Record the actual outcome of a previous decision."""
        for entry in self._decision_journal:
            if entry["id"] == decision_id:
                entry["actual_outcome"] = actual_outcome[:500]
                entry["outcome_recorded_at"] = datetime.now().isoformat()
                entry["success"] = success
                entry["learnings"] = learnings[:500] if learnings else None

                if learnings:
                    self.memory.remember(
                        category="lesson_learned",
                        topic=f"decision_{decision_id}_lesson",
                        content=learnings,
                        source=entry.get("delegated_to", "system"),
                        confidence=0.6 if success else 0.4,
                        tags=["decision", entry.get("delegated_to", "").lower()],
                    )

                self._save_journal()
                return

    def get_decision_effectiveness(self, agent_name: str = None) -> Dict[str, Any]:
        """Analyse decision success rates, optionally filtered by delegated agent."""
        decisions = self._decision_journal
        if agent_name:
            decisions = [d for d in decisions if d.get("delegated_to") == agent_name]

        completed = [d for d in decisions if d.get("success") is not None]
        if not completed:
            return {
                "total_decisions": len(decisions),
                "completed": 0,
                "pending": len(decisions),
                "success_rate": 0,
            }

        successes = sum(1 for d in completed if d.get("success"))
        return {
            "total_decisions": len(decisions),
            "completed": len(completed),
            "pending": len(decisions) - len(completed),
            "success_rate": round(successes / len(completed) * 100, 1),
            "by_agent": self._effectiveness_by_agent(completed),
        }

    def _effectiveness_by_agent(self, decisions: List[Dict]) -> Dict[str, Any]:
        by_agent: Dict = defaultdict(lambda: {"total": 0, "success": 0})
        for d in decisions:
            agent = d.get("delegated_to", "unknown")
            by_agent[agent]["total"] += 1
            if d.get("success"):
                by_agent[agent]["success"] += 1

        return {
            agent: {
                "total": stats["total"],
                "success_rate": round(
                    stats["success"] / max(stats["total"], 1) * 100, 1
                ),
            }
            for agent, stats in by_agent.items()
        }

    # ------------------------------------------------------------------
    # Autonomous Initiatives
    # ------------------------------------------------------------------

    def generate_initiatives(self, llm_callable=None) -> List[Dict[str, Any]]:
        """Generate proactive action proposals based on accumulated intelligence.

        Uses LLM (if provided) to synthesise patterns, decisions, and memory
        into concrete improvement proposals.

        :param llm_callable: ``Callable(system_prompt, user_prompt) -> str`` or ``None``.
        """
        if not llm_callable:
            logger.info("[analytics] No LLM callable — skipping initiative generation")
            return []

        patterns_summary = json.dumps(
            [
                {
                    "desc": p["description"],
                    "conf": p.get("confidence", 0),
                    "action": p.get("suggested_action"),
                }
                for p in self._patterns[-20:]
            ],
            indent=2,
        )
        effectiveness = self.get_decision_effectiveness()
        memory_metrics = self.memory.get_metrics()
        recent_decisions = self._decision_journal[-20:]
        decisions_summary = json.dumps(
            [
                {
                    "decision": d["decision"][:100],
                    "success": d.get("success"),
                    "learnings": (d.get("learnings") or "")[:100],
                }
                for d in recent_decisions
                if d.get("success") is not None
            ],
            indent=2,
        )

        prompt = f"""Based on the following intelligence data, propose 2-4 proactive initiatives
that would improve EDI operations performance.

DETECTED PATTERNS:
{patterns_summary}

DECISION EFFECTIVENESS:
{json.dumps(effectiveness, indent=2)}

CORPORATE MEMORY METRICS:
{json.dumps(memory_metrics, indent=2)}

RECENT DECISION OUTCOMES:
{decisions_summary}

For each initiative, provide:
- title: Short name
- rationale: Why this matters (reference specific data)
- proposed_action: Concrete step to take
- confidence: 0.0-1.0 based on evidence strength
- effort: low/medium/high
- impact: low/medium/high

Return a JSON array.  If no strong initiatives, return [].
"""
        system = (
            "You are a strategic analyst for an EDI operations platform. "
            "Propose concrete, evidence-based improvements.  Be specific. "
            "Return only valid JSON array."
        )

        try:
            response = llm_callable(system, prompt)
            if not response:
                return []

            response = response.strip()
            if response.startswith("```"):
                lines = response.split("\n")
                response = "\n".join(l for l in lines if not l.startswith("```"))

            initiatives = json.loads(response)
            if not isinstance(initiatives, list):
                return []

            for init in initiatives:
                self.memory.remember(
                    category="decision",
                    topic=(
                        f"initiative_{init.get('title', 'unknown')[:40]}"
                        .replace(" ", "_")
                        .lower()
                    ),
                    content=f"{init.get('title')}: {init.get('rationale', '')[:200]}",
                    source="IntelligenceEngine",
                    confidence=init.get("confidence", 0.5),
                    tags=[
                        "initiative",
                        init.get("effort", "medium"),
                        init.get("impact", "medium"),
                    ],
                )

            logger.info(f"[analytics] Generated {len(initiatives)} initiatives")
            return initiatives

        except Exception as e:
            logger.error(f"[analytics] Initiative generation error: {e}")
            return []

    # ------------------------------------------------------------------
    # Intelligence Briefing
    # ------------------------------------------------------------------

    def generate_briefing(self) -> str:
        """Generate the daily intelligence briefing.

        Synthesises recent alerts, predictions, patterns, pending decisions,
        and corporate memory into a formatted briefing string.
        """
        lines: List[str] = []
        today = datetime.now()
        day_name = DAY_NAMES[today.weekday()].title()
        lines.append(
            f"INTELLIGENCE BRIEFING — {today.strftime('%Y-%m-%d')} ({day_name})"
        )
        lines.append("=" * 60)

        # 1. Urgent messages
        lines.append("\nRECENT ALERTS:")
        urgent_messages = self._get_urgent_messages()
        if urgent_messages:
            for msg in urgent_messages[:5]:
                src = msg.get("source") or msg.get("source_bot", "unknown")
                subj = msg.get("subject", "")
                priority = msg.get("priority", "normal")
                lines.append(f"  [{priority.upper()}] {src}: {subj}")
        else:
            lines.append("  No urgent alerts.")

        # 2. Today's predictions
        lines.append("\nTODAY'S PREDICTIONS:")
        predictions = self.predict_today()
        if predictions:
            for pred in predictions[:5]:
                conf = int(pred["confidence"] * 100)
                action = (
                    f" -> {pred['suggested_action']}"
                    if pred.get("suggested_action")
                    else ""
                )
                lines.append(f"  (conf:{conf}%) {pred['prediction']}{action}")
        else:
            lines.append(
                "  No predictions available (insufficient historical data)."
            )

        # 3. High-confidence patterns
        lines.append("\nACTIVE PATTERNS:")
        high_conf = [p for p in self._patterns if p.get("confidence", 0) > 0.6]
        if high_conf:
            for pattern in sorted(
                high_conf, key=lambda p: p.get("confidence", 0), reverse=True
            )[:5]:
                conf = int(pattern.get("confidence", 0) * 100)
                lines.append(f"  (conf:{conf}%) {pattern['description']}")
        else:
            lines.append("  No high-confidence patterns yet.")

        # 4. Pending decisions
        lines.append("\nPENDING DECISIONS TO CHECK:")
        pending = [d for d in self._decision_journal if d.get("success") is None]
        if pending:
            for dec in pending[-5:]:
                lines.append(
                    f"  [{dec['id']}] {dec['delegated_to']}: {dec['decision'][:80]}"
                )
        else:
            lines.append("  No pending decisions.")

        # 5. Knowledge base summary
        lines.append("\nKNOWLEDGE BASE:")
        metrics = self.memory.get_metrics()
        lines.append(
            f"  {metrics['total_facts']} facts stored | "
            f"Avg confidence: {metrics['avg_confidence']:.0%} | "
            f"Growth: +{metrics['knowledge_growth_rate']} this week"
        )

        # 6. Message bus summary (if available)
        bus_stats = self._get_bus_stats()
        if bus_stats.get("total_messages", 0) > 0:
            lines.append("\nMESSAGE BUS:")
            lines.append(
                f"  {bus_stats['total_messages']} total messages | "
                f"{bus_stats.get('undelivered_count', 0)} undelivered"
            )

        # 7. Recommended actions
        lines.append("\nRECOMMENDED ACTIONS:")
        actions: List[str] = []
        if urgent_messages:
            actions.append("Address urgent alerts first")
        for pred in predictions:
            if pred.get("suggested_action"):
                actions.append(pred["suggested_action"])
        if pending:
            actions.append(f"Follow up on {len(pending)} pending decision(s)")
        if actions:
            for action in actions[:5]:
                lines.append(f"  - {action}")
        else:
            lines.append("  - Proceed with standard operations")

        lines.append("")
        lines.append("=" * 60)
        briefing_text = "\n".join(lines)

        # Save daily report
        report_file = REPORTS_DIR / f"briefing_{today.strftime('%Y%m%d')}.txt"
        try:
            report_file.write_text(briefing_text, encoding="utf-8")
        except Exception:
            pass

        return briefing_text

    def _get_urgent_messages(self) -> List[Dict]:
        """Get urgent messages from the bus, adapting to whichever bus API is present."""
        if not self.bus:
            return []
        try:
            if hasattr(self.bus, "get_urgent"):
                return self.bus.get_urgent()
            if hasattr(self.bus, "consume"):
                msgs = self.bus.consume("intelligence_engine")
                return [
                    {
                        "priority": m.get("priority", "normal"),
                        "source": m.get("source", "unknown"),
                        "subject": m.get("subject", ""),
                    }
                    for m in msgs
                    if m.get("priority") == "urgent"
                ]
        except Exception:
            pass
        return []

    def _get_bus_stats(self) -> Dict[str, Any]:
        """Get message bus statistics, gracefully handling different bus implementations."""
        if not self.bus:
            return {}
        try:
            if hasattr(self.bus, "get_stats"):
                return self.bus.get_stats()
        except Exception:
            pass
        return {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _load_work_log(self) -> List[Dict[str, Any]]:
        if WORK_LOG_FILE.exists():
            try:
                return json.loads(WORK_LOG_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _load_feedback(self) -> List[Dict[str, Any]]:
        if FEEDBACK_FILE.exists():
            try:
                return json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
        return []

    def _load_journal(self) -> List[Dict[str, Any]]:
        if JOURNAL_FILE.exists():
            try:
                data = json.loads(JOURNAL_FILE.read_text(encoding="utf-8"))
                return data.get("decisions", [])
            except Exception:
                pass
        return []

    def _save_journal(self) -> None:
        try:
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            JOURNAL_FILE.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "last_updated": datetime.now().isoformat(),
                        "decisions": self._decision_journal[-500:],
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[analytics] Could not save journal: {e}")

    def _load_patterns(self) -> List[Dict[str, Any]]:
        if PATTERNS_FILE.exists():
            try:
                data = json.loads(PATTERNS_FILE.read_text(encoding="utf-8"))
                return data.get("patterns", [])
            except Exception:
                pass
        return []

    def _save_patterns(self) -> None:
        try:
            STORE_DIR.mkdir(parents=True, exist_ok=True)
            PATTERNS_FILE.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "last_updated": datetime.now().isoformat(),
                        "patterns": self._patterns[-200:],
                    },
                    indent=2,
                    default=str,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[analytics] Could not save patterns: {e}")
