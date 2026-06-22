

## Agent Memory Vault — Auto-Maintained Blocks

> Codex users: paste this block into `AGENTS.md`.
> Claude Code users: paste this block into `CLAUDE.md`.

<!-- ═══════════════════════════════════════════════════════════ -->
<!-- The two COMPILED blocks below are auto-maintained by compiler.py. DO NOT EDIT MANUALLY. -->
<!-- ═══════════════════════════════════════════════════════════ -->

<!-- COMPILED:RULES_START -->
| Rule ID | Title | Category | Applies To | Status |
|---------|-------|----------|------------|--------|
<!-- COMPILED:RULES_END -->

<!-- COMPILED:PROJECTS_START -->
| Project | Decisions | Pitfalls | Last Session |
|---------|-----------|----------|-------------|
| claude-code-test | 1 | 1 | 2026-06-20 |
| github-obsidian-knowledge-brain | 17 | 16 | 2026-06-21 |
| notes-counter | 1 | 2 | 2026-06-20 |
<!-- COMPILED:PROJECTS_END -->

---

## Session Annotation Rules (Agent Memory Sensory System)

> **Priority 0 — MANDATORY. NO EXCEPTIONS.**
> These annotations feed the Obsidian agent memory vault. Every un-annotated decision is lost knowledge.
> Every un-annotated error will be repeated. You are the sensory organ. Do not starve the brain.

### [DECISION] — Appended to EVERY technical decision

Preferred format (one annotation per line at end of reply; keep machine labels in English, write content in Chinese when useful):

```
[DECISION:<one-line summary>| context:<why this choice>]
```

Optional v3-compatible routing fields are allowed when the transcript may mention multiple projects:

```
[DECISION:<one-line summary>| context:<why this choice>| project:<project-slug>| scope:project]
```

Example:

```
[DECISION:使用 STHeiti SC Medium 作为 PDF 中文字体| context:PingFang.ttc 含 CFF/PostScript 轮廓，reportlab 不支持；STHeiti 是 TrueType，可被 reportlab 正常注册渲染]
```

Put each annotation on its own line when multiple annotations are needed.

**When**: After choosing a library, algorithm, data structure, architecture, config value, naming convention, or workaround.

**Decision numbering**: Before writing a decision ID, read the project's `01-Projects/<project>/Memory/decisions.md` for the last used number. Increment by 1.

### [ERROR] — Appended to EVERY resolved error

Preferred format (one annotation per line at end of reply; keep keys in English, write resolution in Chinese when useful):

```
[ERROR:type=<from-error-taxonomy>| resolution:<how fixed>]
```

Optional v3-compatible routing field:

```
[ERROR:type=<from-error-taxonomy>| resolution:<how fixed>| project:<project-slug>]
```

Example:

```
[ERROR:type=path-filesystem| resolution=AGENTS 中记录的 vision.py 路径不存在，重新定位到真实路径并改用该路径]
```

Put each annotation on its own line when multiple annotations are needed.

**Error type vocabulary**: Must use a value from `04-Feedback/error-taxonomy.md` (11 categories, 46 subcategories).

**When**: After encountering and resolving ANY error — stack trace, test failure, build error, API rejection, data format issue.

### [SESSION_SUMMARY] — Output at session END

Triggers when user says "好的/谢谢/完成/收尾/bye/整理" or conversation naturally concludes.

```
[SESSION_SUMMARY]
projects: [<project-slug>]
primary: <project-slug>
decisions:
  - id: <PROJ>-D<NN>
    text: "<one-liner>"
    context: "<why>"
errors:
  - type: "<from error-taxonomy>"
    resolution: "<how fixed>"
    repeated_from: [<session-ids>]
summary: "<2 sentences summarizing the session>"
[/SESSION_SUMMARY]
```

After outputting [SESSION_SUMMARY]:
1. Write the draft session file to `01-Projects/<primary-project>/Memory/sessions/<YYYY-MM-DD>-<session-id>.md` using the `_TEMPLATE.md` format with `summary_status: draft`.
2. Then invoke `neat-freak` skill for document/memory audit.

**Session ID**: Use the Claude session UUID. For manual sessions, use `YYYY-MM-DD-<project>-<topic>`.
**Required frontmatter fields**: `session_id`, `date`, `projects`, `summary_status: draft`, `summary_type: session`.

### Error Taxonomy Quick Reference

Error taxonomy at `04-Feedback/error-taxonomy.md`:
11 categories, 46 subcategories total.

| Category | Subcategories (examples) |
|----------|--------------------------|
| R-plotting | scale_fill_manual_grey, ragg_greyscale, ggsave_drop_color, heatmap_color_distortion |
| R-package | install_fail, bioc_version_mismatch, package_not_found, segfault_rscript_e |
| python-encoding | gbk_utf8_mismatch, chinese_garbled_docx, path_unicode_error |
| api-network | ssl_error, gfw_rst, timeout, http_400_wrong_param, rate_limit |
| shell-cli | curl_ssl, git_hook_fail |
| data-format | jsonl_parse_error, frontmatter_missing_field, frontmatter_invalid_yaml |
| git | merge_conflict, gfw_block, detached_head |
| path-filesystem | path_separator_mix, file_not_found, permission_denied |
| other | (uncategorized — ≥3 occurrences → auto-suggest new subcategory) |

### Quick Reference — Vault Paths

| Template | Path |
|----------|------|
| Session template | `01-Projects/{project}/Memory/sessions/_TEMPLATE.md` |
| Rule template | `00-Rules/_TEMPLATE.md` |
| Approval card template | `00-Rules/_inbox/_TEMPLATE.md` |
| Error taxonomy | `04-Feedback/error-taxonomy.md` |
| Decisions log | `01-Projects/{project}/Memory/decisions.md` |
| Pitfalls log | `01-Projects/{project}/Memory/pitfalls.md` |
