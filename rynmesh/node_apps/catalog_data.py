"""Bounded operation contracts adapted from owner Clawpad manifests."""

CATALOG = {
    "resume": {
        "id": "resume",
        "name": "Resume",
        "description": "Build resumes and cover letters; export reviewed text.",
        "operations": {
            "create_resume": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 4000},
                        "target_role": {"type": "string", "maxLength": 4000},
                        "template": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "list_resumes": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                        "offset": {"type": "integer", "minimum": 0, "maximum": 4096},
                        "status": {"type": "string", "maxLength": 4000},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
            "get_resume": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {"resume_id": {"type": "string", "maxLength": 4000}},
                    "required": ["resume_id"],
                    "additionalProperties": False,
                },
            },
            "update_resume": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "resume_id": {"type": "string", "maxLength": 4000},
                        "patch": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string", "maxLength": 4000},
                                "target_role": {"type": "string", "maxLength": 4000},
                                "summary": {"type": "string", "maxLength": 4000},
                                "template": {"type": "string", "maxLength": 4000},
                                "notes": {"type": "string", "maxLength": 4000},
                            },
                            "minProperties": 1,
                        },
                    },
                    "required": ["resume_id", "patch"],
                    "additionalProperties": False,
                },
            },
            "archive_resume": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {"resume_id": {"type": "string", "maxLength": 4000}},
                    "required": ["resume_id"],
                    "additionalProperties": False,
                },
            },
            "restore_resume": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {"resume_id": {"type": "string", "maxLength": 4000}},
                    "required": ["resume_id"],
                    "additionalProperties": False,
                },
            },
            "add_experience": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "resume_id": {"type": "string", "maxLength": 4000},
                        "title": {"type": "string", "maxLength": 4000},
                        "company": {"type": "string", "maxLength": 4000},
                        "start_date": {"type": "string", "maxLength": 4000},
                        "end_date": {"type": "string", "maxLength": 4000},
                        "location": {"type": "string", "maxLength": 4000},
                        "highlights": {
                            "type": "array",
                            "maxItems": 32,
                            "items": {"type": "string", "maxLength": 4000},
                        },
                    },
                    "required": ["resume_id", "title", "company"],
                    "additionalProperties": False,
                },
            },
            "add_education": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "resume_id": {"type": "string", "maxLength": 4000},
                        "school": {"type": "string", "maxLength": 4000},
                        "degree": {"type": "string", "maxLength": 4000},
                        "field_of_study": {"type": "string", "maxLength": 4000},
                        "start_date": {"type": "string", "maxLength": 4000},
                        "end_date": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["resume_id", "school", "degree"],
                    "additionalProperties": False,
                },
            },
            "add_skill": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "resume_id": {"type": "string", "maxLength": 4000},
                        "name": {"type": "string", "maxLength": 4000},
                        "category": {"type": "string", "maxLength": 4000},
                        "level": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["resume_id", "name"],
                    "additionalProperties": False,
                },
            },
            "add_project": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "resume_id": {"type": "string", "maxLength": 4000},
                        "name": {"type": "string", "maxLength": 4000},
                        "role": {"type": "string", "maxLength": 4000},
                        "summary": {"type": "string", "maxLength": 4000},
                        "highlights": {
                            "type": "array",
                            "maxItems": 32,
                            "items": {"type": "string", "maxLength": 4000},
                        },
                    },
                    "required": ["resume_id", "name"],
                    "additionalProperties": False,
                },
            },
            "set_contact_info": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "resume_id": {"type": "string", "maxLength": 4000},
                        "contact_info": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "email": {"type": "string", "maxLength": 4000},
                                "phone": {"type": "string", "maxLength": 4000},
                                "location": {"type": "string", "maxLength": 4000},
                                "website": {"type": "string", "maxLength": 4000},
                                "linkedin": {"type": "string", "maxLength": 4000},
                            },
                        },
                    },
                    "required": ["resume_id", "contact_info"],
                    "additionalProperties": False,
                },
            },
            "create_cover_letter": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "resume_id": {"type": "string", "maxLength": 4000},
                        "title": {"type": "string", "maxLength": 4000},
                        "body": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["resume_id", "title", "body"],
                    "additionalProperties": False,
                },
            },
            "export_resume_text": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {"resume_id": {"type": "string", "maxLength": 256}},
                    "required": ["resume_id"],
                    "additionalProperties": False,
                },
            },
        },
        "source": {
            "repository": "owner Clawpad",
            "commit": "1bf21d840",
            "manifest_sha256": "d67a24449e945e67f0d956fe441d694c93a46465d95eab9e80be52db2c1610f2",
        },
    },
    "recruit": {
        "id": "recruit",
        "name": "Recruit",
        "description": "Manage job requests and candidate stages. Budgets quote Ryncoin; payments use Exchange.",
        "operations": {
            "create_job": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 4000},
                        "description": {"type": "string", "maxLength": 4000},
                        "department": {"type": "string", "maxLength": 4000},
                        "location": {"type": "string", "maxLength": 4000},
                        "requirements": {
                            "type": "array",
                            "maxItems": 32,
                            "items": {"type": "string", "maxLength": 4000},
                        },
                        "budget_ryncoin": {
                            "type": "string",
                            "pattern": "^(0|[1-9][0-9]{0,8})(\\.[0-9]{1,6})?$",
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "list_jobs": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                        "offset": {"type": "integer", "minimum": 0, "maximum": 4096},
                        "status": {"type": "string", "maxLength": 4000},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
            "get_job": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {"job_id": {"type": "string", "maxLength": 4000}},
                    "required": ["job_id"],
                    "additionalProperties": False,
                },
            },
            "create_candidate": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 4000},
                        "email": {"type": "string", "maxLength": 4000},
                        "phone": {"type": "string", "maxLength": 4000},
                        "job_id": {"type": "string", "maxLength": 4000},
                        "source": {"type": "string", "maxLength": 4000},
                        "resume_text": {"type": "string", "maxLength": 8192},
                    },
                    "required": ["name", "job_id"],
                    "additionalProperties": False,
                },
            },
            "list_candidates": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                        "offset": {"type": "integer", "minimum": 0, "maximum": 4096},
                        "job_id": {"type": "string", "maxLength": 4000},
                        "stage": {"type": "string", "maxLength": 4000},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
            "get_candidate": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {"candidate_id": {"type": "string", "maxLength": 4000}},
                    "required": ["candidate_id"],
                    "additionalProperties": False,
                },
            },
            "advance_candidate": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string", "maxLength": 4000},
                        "stage": {"enum": ["screening", "interview", "offer", "hired"]},
                        "notes": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["candidate_id", "stage"],
                    "additionalProperties": False,
                },
            },
            "reject_candidate": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "candidate_id": {"type": "string", "maxLength": 4000},
                        "reason": {"type": "string", "maxLength": 4000},
                        "notes": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["candidate_id"],
                    "additionalProperties": False,
                },
            },
            "get_pipeline": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {"job_id": {"type": "string", "maxLength": 4000}},
                    "required": [],
                    "additionalProperties": False,
                },
            },
        },
        "source": {
            "repository": "owner Clawpad",
            "commit": "1bf21d840",
            "manifest_sha256": "4e123726d20914c7bb1ccc433c209928dae7f104e4f1a166c942f2248ce7e90c",
        },
    },
    "desk": {
        "id": "desk",
        "name": "Desk",
        "description": "Track service requests, private comments and resolutions.",
        "operations": {
            "create_ticket": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "maxLength": 4000},
                        "description": {"type": "string", "maxLength": 4000},
                        "requester_name": {"type": "string", "maxLength": 4000},
                        "requester_email": {"type": "string", "maxLength": 4000},
                        "priority": {"type": "string", "maxLength": 4000},
                        "tags": {
                            "type": "array",
                            "maxItems": 32,
                            "items": {"type": "string", "maxLength": 4000},
                        },
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
            "list_tickets": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {
                        "status": {"type": "string", "maxLength": 4000},
                        "priority": {"type": "string", "maxLength": 4000},
                        "tag": {"type": "string", "maxLength": 4000},
                        "include_closed": {"type": "boolean"},
                        "query": {"type": "string", "maxLength": 4000},
                        "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                        "offset": {"type": "integer", "minimum": 0, "maximum": 4096},
                    },
                    "required": [],
                    "additionalProperties": False,
                },
            },
            "get_ticket": {
                "effect": "read",
                "schema": {
                    "type": "object",
                    "properties": {"ticket_id": {"type": "string", "maxLength": 4000}},
                    "required": ["ticket_id"],
                    "additionalProperties": False,
                },
            },
            "close_ticket": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "string", "maxLength": 4000},
                        "resolution_note": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["ticket_id"],
                    "additionalProperties": False,
                },
            },
            "add_comment": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "string", "maxLength": 4000},
                        "body": {"type": "string", "maxLength": 4000},
                        "author_name": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["ticket_id", "body"],
                    "additionalProperties": False,
                },
            },
            "reopen_ticket": {
                "effect": "write",
                "schema": {
                    "type": "object",
                    "properties": {
                        "ticket_id": {"type": "string", "maxLength": 4000},
                        "reason": {"type": "string", "maxLength": 4000},
                    },
                    "required": ["ticket_id"],
                    "additionalProperties": False,
                },
            },
        },
        "source": {
            "repository": "owner Clawpad",
            "commit": "1bf21d840",
            "manifest_sha256": "32848c2137c697e3d3f878365930ac93a36b777cbbe9bcb39280bdb441503b1a",
        },
    },
}
