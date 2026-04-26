"""Skill modules — pluggable tools for the LLM agents.

To add a new skill:

1. Create a file under :mod:`virtual_dev.skills.builtin` (or a sibling
   namespace package once we add user-supplied dirs).
2. Decorate one async function with
   :func:`virtual_dev.application.services.skills.skill`. Provide a
   name, a description (LLM-readable!), a JSON-schema, and tags.
3. The startup container calls
   :func:`virtual_dev.application.services.skills.discover_builtin_skills`
   which imports every module here, the decorator registers the skill,
   and any agent that asks for skills with that tag will see it.

That's it. No registry edit, no MCP wiring, no agent code changes.
"""
