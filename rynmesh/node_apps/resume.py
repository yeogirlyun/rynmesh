"""Pure Resume business methods migrated from owner Clawpad; no disk or process access.

Source: apps/resume/src/core/engine.py, Clawpad 1bf21d840.
Original SHA256: 8823bd16af394925ccc995a962c94fc716984d7264c9c2edd078b1ecea011db9. Persistence and public dispatch belong to Rynmesh.
"""

from __future__ import annotations

import uuid
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

SECTION_ORDER = [
    "contact",
    "summary",
    "experience",
    "education",
    "skills",
    "projects",
    "certifications",
    "references",
    "languages",
    "custom_sections",
]

TEMPLATES = [
    {"id": "modern", "name": "Modern", "style": "clean two-column"},
    {"id": "classic", "name": "Classic", "style": "traditional serif"},
    {"id": "minimal", "name": "Minimal", "style": "single-column compact"},
    {"id": "executive", "name": "Executive", "style": "leadership-focused"},
    {"id": "bold", "name": "Bold", "style": "high-contrast headline"},
    {"id": "canva_clean", "name": "Canva Clean", "style": "visual blocks and spacing"},
]

TEMPLATE_IDS = {item["id"] for item in TEMPLATES}


class EngineError(Exception):
    def __init__(self, code: str, message: str, recoverable: bool = True, hint: str | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.recoverable = recoverable
        self.hint = hint

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "code": self.code,
            "message": self.message,
            "recoverable": self.recoverable,
        }
        if self.hint:
            payload["hint"] = self.hint
        return payload


class ResumeEngine:
    _BUCKETS = ["resumes", "exports", "settings"]

    def __init__(self, state=None):
        self.state = (
            deepcopy(state)
            if state is not None
            else {"records": {}, "counters": {}, "activity": [], "history": []}
        )
        self._ensure_state_shape()

    def _ensure_state_shape(self) -> None:
        records = self.state.setdefault("records", {})
        for bucket in self._BUCKETS:
            if not isinstance(records.get(bucket), list):
                records[bucket] = []
        self.state.setdefault("activity", [])
        self.state.setdefault("history", [])
        self.state.setdefault("counters", {})
        self.state.setdefault("modified", False)
        self.state.setdefault("last_action", None)
        if not records["settings"]:
            now = self._now()
            records["settings"].append(
                {
                    "id": "settings_default",
                    "default_template": "modern",
                    "accent_color": "navy",
                    "paper_size": "letter",
                    "include_references": True,
                    "active_resume_id": None,
                    "created_at": now,
                    "updated_at": now,
                }
            )

    def _now(self) -> str:
        return datetime.now(timezone.utc).isoformat()

    def _next_id(self, prefix: str) -> str:
        self.state["counters"].setdefault(prefix, 0)
        self.state["counters"][prefix] += 1
        return f"{prefix}_{self.state['counters'][prefix]:04d}_{uuid.uuid4().hex[:6]}"

    def _ensure_bucket(self, bucket: str) -> list[dict[str, Any]]:
        self._ensure_state_shape()
        rows = self.state["records"].setdefault(bucket, [])
        if not isinstance(rows, list):
            raise EngineError(
                "internal_error", f"{bucket} storage is corrupted.", recoverable=False
            )
        return rows

    def _settings_record(self) -> dict[str, Any]:
        return self._ensure_bucket("settings")[0]

    def _touch(self, row: dict[str, Any]) -> None:
        row["updated_at"] = self._now()
        self.state["modified"] = True

    def _coerce_limit(self, payload: dict[str, Any], default: int = 50, maximum: int = 200) -> int:
        raw = payload.get("limit", default)
        if raw is None:
            return default
        if isinstance(raw, bool):
            raise EngineError("invalid_params", "limit must be an integer.")
        if not isinstance(raw, int):
            try:
                raw = int(str(raw))
            except (TypeError, ValueError) as exc:
                raise EngineError("invalid_params", "limit must be an integer.") from exc
        if raw <= 0:
            raise EngineError("invalid_params", "limit must be greater than zero.")
        return min(raw, maximum)

    def _coerce_offset(self, payload: dict[str, Any]) -> int:
        raw = payload.get("offset", 0)
        if raw is None:
            return 0
        if isinstance(raw, bool):
            raise EngineError("invalid_params", "offset must be an integer.")
        if not isinstance(raw, int):
            try:
                raw = int(str(raw))
            except (TypeError, ValueError) as exc:
                raise EngineError("invalid_params", "offset must be an integer.") from exc
        if raw < 0:
            raise EngineError("invalid_params", "offset cannot be negative.")
        return raw

    def _require_resume(self, resume_id: Any, *, allow_archived: bool = True) -> dict[str, Any]:
        if not isinstance(resume_id, str) or not resume_id.strip():
            raise EngineError("invalid_params", "resume_id must be a non-empty string.")
        for resume in self._ensure_bucket("resumes"):
            if resume.get("id") == resume_id:
                if not allow_archived and resume.get("status") == "archived":
                    raise EngineError(
                        "conflict",
                        "Archived resumes cannot be edited.",
                        hint="Restore the resume before changing its sections.",
                    )
                return resume
        raise EngineError(
            "resume_not_found",
            f"Resume {resume_id} was not found.",
            hint="List resumes to discover valid ids.",
        )

    def _require_template(self, template_id: Any) -> str:
        if not isinstance(template_id, str) or not template_id.strip():
            raise EngineError("template_not_found", f"{template_id} is not a supported template.")
        lowered = template_id.strip().lower()
        for index, item in enumerate(TEMPLATES, start=1):
            item_id = str(item["id"]).lower()
            item_name = str(item["name"]).lower()
            aliases = {
                item_id,
                item_name,
                item_name.replace(" ", "_"),
                item_name.replace(" ", ""),
                f"template {index}",
                f"template_{index}",
                f"template{index}",
                f"resume template {index}",
            }
            if lowered in aliases or item_name in lowered or item_id in lowered.replace(" ", "_"):
                return item["id"]
        raise EngineError("template_not_found", f"{template_id} is not a supported template.")

    def _ensure_unique_resume_name(self, name: str, *, ignore_id: str | None = None) -> None:
        lowered = name.strip().lower()
        for resume in self._ensure_bucket("resumes"):
            if ignore_id and resume.get("id") == ignore_id:
                continue
            if str(resume.get("name", "")).strip().lower() == lowered:
                raise EngineError(
                    "duplicate_record",
                    f"{name} already exists.",
                    hint="Rename the resume or update the existing one instead.",
                )

    def _default_resume_record(self, name: str, target_role: str, template: str) -> dict[str, Any]:
        now = self._now()
        return {
            "id": self._next_id("resume"),
            "name": name,
            "target_role": target_role,
            "template": template,
            "summary": "",
            "notes": "",
            "status": "active",
            "contact_info": {},
            "section_order": deepcopy(SECTION_ORDER),
            "experience": [],
            "education": [],
            "skills": [],
            "projects": [],
            "certifications": [],
            "references": [],
            "languages": [],
            "custom_sections": [],
            "cover_letter": None,
            "created_at": now,
            "updated_at": now,
            "last_opened": now,
        }

    def _set_active_resume(self, resume_id: str | None) -> None:
        settings = self._settings_record()
        settings["active_resume_id"] = resume_id
        self._touch(settings)

    def _resume_counts(self, resume: dict[str, Any]) -> dict[str, int]:
        return {
            "experience": len(resume.get("experience", [])),
            "education": len(resume.get("education", [])),
            "skills": len(resume.get("skills", [])),
            "projects": len(resume.get("projects", [])),
            "certifications": len(resume.get("certifications", [])),
            "references": len(resume.get("references", [])),
            "languages": len(resume.get("languages", [])),
            "custom_sections": len(resume.get("custom_sections", [])),
        }

    def _resume_to_md(self, resume: dict[str, Any]) -> list[str]:
        lines: list[str] = [f"## {resume['name']}", ""]
        if resume.get("target_role"):
            lines.extend([f"**{resume['target_role']}**", ""])
        contact = resume.get("contact_info", {})
        if contact:
            lines.extend([" | ".join((str(v) for v in contact.values() if v)), ""])
        if resume.get("summary"):
            lines.extend(["### Summary", "", resume["summary"], ""])
        if resume.get("experience"):
            lines.extend(["### Experience", ""])
            for exp in resume["experience"]:
                dates = f"{exp.get('start_date', '')} - {exp.get('end_date', 'Present')}"
                loc = f" ({exp.get('location')})" if exp.get("location") else ""
                lines.extend([f"**{exp['title']}** - {exp['company']}{loc}", f"_{dates}_"])
                for h in exp.get("highlights", []):
                    lines.append(f"- {h}")
                lines.append("")
        if resume.get("education"):
            lines.extend(["### Education", ""])
            for edu in resume["education"]:
                label = f"**{edu['school']}** - {edu['degree']}"
                if edu.get("field_of_study"):
                    label += f", {edu['field_of_study']}"
                lines.append(label)
                if edu.get("start_date") or edu.get("end_date"):
                    lines.append(f"_{edu.get('start_date', '')} - {edu.get('end_date', '')}_")
                lines.append("")
        if resume.get("skills"):
            lines.extend(["### Skills", ""])
            by_cat: dict[str, list[str]] = {}
            for skill in resume["skills"]:
                cat = skill.get("category", "General")
                by_cat.setdefault(cat, []).append(skill["name"])
            for cat, names in by_cat.items():
                lines.append(f"**{cat}**: {', '.join(names)}")
            lines.append("")
        if resume.get("projects"):
            lines.extend(["### Projects", ""])
            for project in resume["projects"]:
                lines.append(f"**{project['name']}**")
                if project.get("summary"):
                    lines.append(project["summary"])
                for h in project.get("highlights", []):
                    lines.append(f"- {h}")
                lines.append("")
        if resume.get("certifications"):
            lines.extend(["### Certifications", ""])
            for cert in resume["certifications"]:
                lines.append(f"- **{cert['name']}** - {cert['issuer']}")
            lines.append("")
        if resume.get("languages"):
            lines.extend(["### Languages", ""])
            for lang in resume["languages"]:
                lines.append(f"- {lang['name']} ({lang['proficiency']})")
            lines.append("")
        for custom in resume.get("custom_sections", []):
            lines.extend([f"### {custom['title']}", ""])
            if custom.get("body"):
                lines.extend([custom["body"], ""])
            if custom.get("table"):
                table = custom["table"]
                if table.get("headers"):
                    lines.append("| " + " | ".join(table["headers"]) + " |")
                    lines.append("| " + " | ".join(("---" for _ in table["headers"])) + " |")
                for row in table.get("rows", []):
                    lines.append("| " + " | ".join((str(c) for c in row)) + " |")
                lines.append("")
            if custom.get("items"):
                for item in custom["items"]:
                    lines.append(f"- {item}")
                lines.append("")
        return lines

    def _handle_create_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        name = str(payload["name"]).strip()
        if not name:
            raise EngineError("invalid_params", "name cannot be blank.")
        template = self._require_template(
            str(
                payload.get("template")
                or self._settings_record().get("default_template")
                or "modern"
            )
        )
        self._ensure_unique_resume_name(name)
        record = self._default_resume_record(name, str(payload.get("target_role") or ""), template)
        self._ensure_bucket("resumes").append(record)
        self.state["modified"] = True
        self._set_active_resume(record["id"])
        return {"record": deepcopy(record)}

    def _handle_list_resumes(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = deepcopy(self._ensure_bucket("resumes"))
        status = payload.get("status")
        if isinstance(status, str) and status.strip() and (status.strip().lower() != "all"):
            rows = [
                row for row in rows if str(row.get("status", "")).lower() == status.strip().lower()
            ]
        rows.sort(key=lambda row: row.get("updated_at", ""), reverse=True)
        limit = self._coerce_limit(payload)
        offset = self._coerce_offset(payload)
        return {"items": rows[offset : offset + limit], "total": len(rows)}

    def _handle_get_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = deepcopy(self._require_resume(payload["resume_id"]))
        resume["counts"] = self._resume_counts(resume)
        self._set_active_resume(resume["id"])
        return {"record": resume}

    def _handle_update_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"], allow_archived=False)
        patch = payload["patch"]
        if not isinstance(patch, dict):
            raise EngineError("invalid_params", "patch must be an object.")
        updated_fields: list[str] = []
        if "name" in patch:
            name = str(patch["name"]).strip()
            if not name:
                raise EngineError("invalid_params", "name cannot be blank.")
            self._ensure_unique_resume_name(name, ignore_id=resume["id"])
            resume["name"] = name
            updated_fields.append("name")
        if "target_role" in patch:
            resume["target_role"] = str(patch["target_role"]).strip()
            updated_fields.append("target_role")
        if "summary" in patch:
            resume["summary"] = str(patch["summary"])
            updated_fields.append("summary")
        if "notes" in patch:
            resume["notes"] = str(patch["notes"])
            updated_fields.append("notes")
        if "template" in patch:
            template = self._require_template(str(patch["template"]).strip())
            resume["template"] = template
            updated_fields.append("template")
        if not updated_fields:
            raise EngineError(
                "invalid_params", "patch did not include any supported resume fields."
            )
        self._touch(resume)
        return {"record": deepcopy(resume), "updated_fields": updated_fields}

    def _handle_archive_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"])
        if resume.get("status") == "archived":
            raise EngineError(
                "conflict",
                "This resume is already archived.",
                hint="Restore it before archiving it again.",
            )
        resume["status"] = "archived"
        self._touch(resume)
        return {"record": deepcopy(resume), "archived": True}

    def _handle_restore_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"])
        if resume.get("status") != "archived":
            raise EngineError("invalid_params", "Only archived resumes can be restored.")
        resume["status"] = "active"
        self._touch(resume)
        return {"record": deepcopy(resume), "restored": True}

    def _handle_add_experience(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"], allow_archived=False)
        title = str(payload["title"]).strip()
        company = str(payload["company"]).strip()
        if not title or not company:
            raise EngineError("invalid_params", "title and company cannot be blank.")
        record = {
            "id": self._next_id("experience"),
            "title": title,
            "company": company,
            "start_date": str(payload.get("start_date") or "").strip(),
            "end_date": str(payload.get("end_date") or "").strip(),
            "location": str(payload.get("location") or "").strip(),
            "highlights": [
                str(item).strip() for item in payload.get("highlights") or [] if str(item).strip()
            ],
        }
        resume["experience"].append(record)
        self._touch(resume)
        return {"record": deepcopy(record)}

    def _handle_add_education(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"], allow_archived=False)
        school = str(payload["school"]).strip()
        degree = str(payload["degree"]).strip()
        if not school or not degree:
            raise EngineError("invalid_params", "school and degree cannot be blank.")
        record = {
            "id": self._next_id("education"),
            "school": school,
            "degree": degree,
            "field_of_study": str(payload.get("field_of_study") or "").strip(),
            "start_date": str(payload.get("start_date") or "").strip(),
            "end_date": str(payload.get("end_date") or "").strip(),
        }
        resume["education"].append(record)
        self._touch(resume)
        return {"record": deepcopy(record)}

    def _handle_add_skill(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"], allow_archived=False)
        name = str(payload["name"]).strip()
        if not name:
            raise EngineError("invalid_params", "name cannot be blank.")
        if any(
            (
                str(skill.get("name", "")).strip().lower() == name.lower()
                for skill in resume["skills"]
            )
        ):
            raise EngineError(
                "duplicate_record",
                f"{name} already exists.",
                hint="Update or remove the existing skill instead.",
            )
        record = {
            "id": self._next_id("skill"),
            "name": name,
            "category": str(payload.get("category") or "").strip(),
            "level": str(payload.get("level") or "").strip(),
        }
        resume["skills"].append(record)
        self._touch(resume)
        return {"record": deepcopy(record)}

    def _handle_add_project(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"], allow_archived=False)
        name = str(payload["name"]).strip()
        if not name:
            raise EngineError("invalid_params", "name cannot be blank.")
        record = {
            "id": self._next_id("project"),
            "name": name,
            "role": str(payload.get("role") or "").strip(),
            "summary": str(payload.get("summary") or ""),
            "highlights": [
                str(item).strip() for item in payload.get("highlights") or [] if str(item).strip()
            ],
        }
        resume["projects"].append(record)
        self._touch(resume)
        return {"record": deepcopy(record)}

    def _handle_set_contact_info(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"], allow_archived=False)
        contact_info = payload["contact_info"]
        if not isinstance(contact_info, dict):
            raise EngineError("invalid_params", "contact_info must be an object.")
        resume["contact_info"] = {
            key: str(value).strip()
            for key, value in contact_info.items()
            if str(key).strip() and value not in (None, "")
        }
        self._touch(resume)
        return {"record": deepcopy(resume["contact_info"])}

    def _handle_create_cover_letter(self, payload: dict[str, Any]) -> dict[str, Any]:
        resume = self._require_resume(payload["resume_id"], allow_archived=False)
        if resume.get("cover_letter"):
            raise EngineError(
                "duplicate_record",
                "This resume already has a cover letter.",
                hint="Update the existing cover letter instead.",
            )
        title = str(payload["title"]).strip()
        body = str(payload["body"]).strip()
        if not title or not body:
            raise EngineError("invalid_params", "title and body cannot be blank.")
        resume["cover_letter"] = {
            "id": self._next_id("cover_letter"),
            "title": title,
            "body": body,
            "created_at": self._now(),
            "updated_at": self._now(),
        }
        self._touch(resume)
        return {"record": deepcopy(resume["cover_letter"])}
