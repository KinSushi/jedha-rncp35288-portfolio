# Repository Standard

This repository follows the public KinSushi portfolio standard for regulated and data-intensive systems.

## Visual standard

Each portfolio repository should contain:

- a banner SVG in `assets/`;
- a centered README header;
- stack badges;
- an executive summary;
- a documentation index;
- architecture or workflow section;
- quickstart commands;
- public-safety rules;
- portfolio signal section.

## Engineering standard

Minimum expected evidence:

- reproducible setup;
- tests or structural checks;
- CI workflow;
- clean folder structure;
- `.gitignore` for generated/private files;
- `.gitattributes` for line-ending stability;
- no generated artifacts committed unless explicitly documented.

## Regulated-data public safety

Never commit:

- real banking data;
- real insurance or health data;
- real client or employer data;
- CVs, cover letters or job trackers;
- private school documents, grades or exam content;
- secrets, tokens, private IPs or hostnames;
- production decisioning claims.

## Review checklist

Before merging or pushing:

- [ ] README still renders cleanly;
- [ ] banner renders;
- [ ] CI structural check passes;
- [ ] no private school material is present;
- [ ] no private/application material is present;
- [ ] docs match actual evidence;
- [ ] screenshots are sanitized.
