# Third-Party Notices

## Scope of the Project License

Original code authored for KinArc, to the extent its contributors have the
right to license it, is available under the [MIT License](LICENSE).

The project's MIT License does not replace or modify the licenses governing
third-party code, datasets, fonts, models, documentation, or other assets that
KinArc uses, references, modifies, or includes. Those materials remain the
property of their respective copyright holders and are subject to the licenses,
copyright notices, NOTICE files, and terms accompanying their specific versions.

When using, modifying, or redistributing third-party materials, retain all required
notices and license texts and comply with their applicable conditions. Inclusion
in this repository or use through KinArc does not relicense third-party
materials under the project's MIT License. No additional rights to project names,
third-party trademarks, or brand identifiers are granted by this notice.

## Coverage and Sources

This document is an inventory of direct dependencies and identified external
resources to support maintenance and distribution. Python runtime versions below
match the current `uv.lock`; license identifiers were checked against installed
distribution metadata for those same versions. JavaScript versions and license
identifiers were checked against the respective `package-lock.json` files.

Package metadata describes declared licenses; it is not a substitute for examining
the source, embedded components, and license files in each distribution.

This inventory does not yet cover all transitive dependencies. A file-by-file
provenance review of business data, prompts, skills, and historically copied code
has not been completed. Source archives, wheels, Web bundles, and container images
must include the license texts, copyright notices, and NOTICE files required by
the components actually distributed. Names and links in this inventory alone do
not establish that all redistribution requirements have been met.

## Direct Python Runtime Dependencies

Declarations: [pyproject.toml](pyproject.toml). Versions: [uv.lock](uv.lock).
Links identify the corresponding package releases. Consult LICENSE, COPYING,
NOTICE, and other applicable files included in each specific release.

| Component | Version | Declared license | Release |
| --- | --- | --- | --- |
| aiobotocore | 3.9.0 | Apache-2.0 | [PyPI](https://pypi.org/project/aiobotocore/3.9.0/) |
| aiofiles | 25.1.0 | Apache-2.0 | [PyPI](https://pypi.org/project/aiofiles/25.1.0/) |
| aiohttp | 3.14.3 | Apache-2.0 AND MIT | [PyPI](https://pypi.org/project/aiohttp/3.14.3/) |
| alembic | 1.19.1 | MIT | [PyPI](https://pypi.org/project/alembic/1.19.1/) |
| asyncmy | 0.2.13 | Apache-2.0 | [PyPI](https://pypi.org/project/asyncmy/0.2.13/) |
| cachetools | 7.0.3 | MIT | [PyPI](https://pypi.org/project/cachetools/7.0.3/) |
| croniter | 6.2.4 | MIT | [PyPI](https://pypi.org/project/croniter/6.2.4/) |
| docling | 2.132.0 | MIT | [PyPI](https://pypi.org/project/docling/2.132.0/) |
| fastapi | 0.135.1 | MIT | [PyPI](https://pypi.org/project/fastapi/0.135.1/) |
| greenlet | 3.5.4 | MIT AND PSF-2.0 | [PyPI](https://pypi.org/project/greenlet/3.5.4/) |
| httpx | 0.28.1 | BSD-3-Clause | [PyPI](https://pypi.org/project/httpx/0.28.1/) |
| litellm (with the google extra) | 1.95.0 | MIT; dependencies introduced by extras require separate review | [PyPI](https://pypi.org/project/litellm/1.95.0/) |
| mcp | 1.26.0 | MIT | [PyPI](https://pypi.org/project/mcp/1.26.0/) |
| opentelemetry-exporter-otlp-proto-http | 1.44.0 | Apache-2.0 | [PyPI](https://pypi.org/project/opentelemetry-exporter-otlp-proto-http/1.44.0/) |
| opentelemetry-sdk | 1.44.0 | Apache-2.0 | [PyPI](https://pypi.org/project/opentelemetry-sdk/1.44.0/) |
| puremagic | 2.2.0 | MIT | [PyPI](https://pypi.org/project/puremagic/2.2.0/) |
| pydantic | 2.12.5 | MIT | [PyPI](https://pypi.org/project/pydantic/2.12.5/) |
| python-dotenv | 1.2.2 | BSD-3-Clause | [PyPI](https://pypi.org/project/python-dotenv/1.2.2/) |
| python-multipart | 0.0.22 | Apache-2.0 | [PyPI](https://pypi.org/project/python-multipart/0.0.22/) |
| PyYAML | 6.0.3 | MIT | [PyPI](https://pypi.org/project/PyYAML/6.0.3/) |
| rank-bm25 | 0.2.2 | Apache-2.0 (declared as Apache2.0 in metadata) | [PyPI](https://pypi.org/project/rank-bm25/0.2.2/) |
| redis (Python client) | 7.2.1 | MIT | [PyPI](https://pypi.org/project/redis/7.2.1/) |
| SQLAlchemy | 2.0.51 | MIT | [PyPI](https://pypi.org/project/SQLAlchemy/2.0.51/) |
| uvicorn | 0.52.1 | BSD-3-Clause | [PyPI](https://pypi.org/project/uvicorn/0.52.1/) |

The license of the `redis` Python client does not determine the license of Redis
Server. SDK licenses likewise do not govern the remote services they access.
Model weights downloaded by Docling or other components require separate review.

## Direct Web Dependencies and Tools

Declarations: [web/package.json](web/package.json). Versions and declared licenses:
[web/package-lock.json](web/package-lock.json). Review transitive dependencies
included in the final bundle as well.

| Component | Version | Declared license |
| --- | --- | --- |
| vue | 3.5.44 | MIT |
| vue-router | 4.6.4 | MIT |
| vue-stream-markdown | 1.1.0 | MIT |
| katex | 0.18.10 | MIT |
| @types/node | 22.20.1 | MIT |
| @vitejs/plugin-vue | 6.0.8 | MIT |
| @vue/test-utils | 2.4.11 | MIT |
| jsdom | 26.1.0 | MIT |
| prettier | 3.8.3 | MIT |
| typescript | 5.9.3 | Apache-2.0 |
| vite | 7.3.7 | MIT |
| vitest | 4.1.12 | MIT |
| vue-tsc | 3.3.9 | MIT |

Upstream information and license files can be located through the corresponding
package version in the [npm Registry](https://www.npmjs.com/). Lockfile license
fields do not replace the full notices included in package distributions.

## Python Development, Build, and Commit Tools

These tools are not all production runtime dependencies. If a distribution
includes them, their licensing requirements still apply. Python declarations are
in `pyproject.toml`; commit tools are declared in [package.json](package.json) and
[package-lock.json](package-lock.json).

| Component | Version reviewed | Declared license |
| --- | --- | --- |
| mypy | 2.3.0 | MIT |
| pre-commit | 4.6.1 | MIT |
| pytest | 9.1.1 | MIT |
| pytest-asyncio | 1.4.0 | Apache-2.0 |
| ruff | 0.16.1 | MIT |
| types-aiobotocore | 3.9.0 | MIT |
| types-aiofiles | 25.1.0.20260518 | Apache-2.0 |
| types-croniter | 6.2.4.20260711 | Apache-2.0 |
| types-pyyaml | 6.0.12.20260724 | Apache-2.0 |
| setuptools | 84.0.0 (locally installed; build environments resolve it separately) | MIT |
| @commitlint/cli | 21.2.1 | MIT |
| @commitlint/config-conventional | 21.2.0 | MIT |

## Benchmarks, Derived Materials, and External Data

### Tau2 Bench

- Upstream: [sierra-research/tau2-bench](https://github.com/sierra-research/tau2-bench).
- Integration version: `v1.0.1`; see the [integration guide](benchmarks/tau2bench/README.md).
- Upstream root license: [MIT License](https://github.com/sierra-research/tau2-bench/blob/v1.0.1/LICENSE).
- Upstream copyright: `Copyright (c) 2025 Sierra Research`.
- Retained upstream license: [licenses/tau2-bench/LICENSE](licenses/tau2-bench/LICENSE).
- Related locations: `benchmarks/tau2bench/`, its `patches/` directory,
  `evolution_tasks/tau3_banking/`, and related policies, tool definitions, and data
  in the Banking applications. Distinguish project-authored adapters from upstream materials.

Upstream source cloned by setup scripts must retain its LICENSE. Copied, modified,
or redistributed upstream materials must retain the applicable copyright notice
and full MIT text. Check databases, documents, tasks, and attachments for separate
notices rather than assuming the root code license covers every asset.

### EvoAgentBench

- Upstream: [EverMind-AI/EvoAgentBench](https://github.com/EverMind-AI/EvoAgentBench).
- Recorded revision: `948a17288782d5120778da16b4cf1cad9305d8b4`.
- Website/root license: [MIT License](https://github.com/EverMind-AI/EvoAgentBench/blob/948a17288782d5120778da16b4cf1cad9305d8b4/LICENSE),
  with `Copyright (c) 2026 EverMind`.
- Benchmark implementation under `benchmark/`: Apache License 2.0, with
  `Copyright 2026 NPC-Work` in its license text. The matching upstream
  [LICENSE](licenses/evoagentbench/LICENSE) is retained locally.
- Retained upstream [third-party notices](licenses/evoagentbench/THIRD_PARTY_NOTICES.md)
  identify MIT-licensed BrowseComp-Plus and ClawWork source incorporated or adapted
  by the benchmark, including their original copyright and permission notices.
- The [EvoAgentBench Hugging Face dataset card](https://huggingface.co/datasets/EverMind-AI/EvoAgentBench)
  declares Apache-2.0 separately from the website's MIT license.
- Related locations: `benchmarks/evoagentbench/`,
  `evolution_tasks/evoagentbench_knowledge/`,
  `evolution_tasks/evoagentbench_research/`, and the corresponding applications.

The root MIT license does not describe the benchmark implementation. Project-authored
integration code remains covered by this project's MIT License to the extent its
contributors have the right to license it; invoking an external benchmark adapter
does not relicense the benchmark or its data.

When redistributing copied or modified benchmark implementation, include its full
Apache License 2.0 text, retain applicable copyrights and attribution, mark modified
files, and preserve applicable upstream NOTICE content if supplied. Also retain
the relevant MIT copyright and permission notices for incorporated BrowseComp-Plus
and ClawWork source. Record the actual source revision distributed.

The upstream code and dataset-card licenses do not replace the terms governing original datasets,
reference attachments, search corpora, indexes, or model weights. Provenance and
split details are recorded in the [dataset guide](benchmarks/evoagentbench/docs/DATASETS.md).
Derived experiment archives are distributed separately in the
[KinArc-Evolution Hugging Face repository](https://huggingface.co/datasets/YiXin-AILab/KinArc-Evolution);
their release notices must identify the third-party materials actually included.

### GDPval, BrowseComp-Plus, and Other Data

| Material | Source | Review status and scope |
| --- | --- | --- |
| GDPval | [openai/gdpval](https://huggingface.co/datasets/openai/gdpval) | Review the dataset card, attachments, and terms for the downloaded version. A single license identifier covering all data and attachments was not confirmed in this review. |
| BrowseComp-Plus | [texttron/BrowseComp-Plus](https://github.com/texttron/BrowseComp-Plus); [Tevatron/browsecomp-plus](https://huggingface.co/datasets/Tevatron/browsecomp-plus) | The code repository's root [LICENSE](https://github.com/texttron/BrowseComp-Plus/blob/main/LICENSE) is MIT, with `Copyright (c) 2025 texttron`. Datasets, corpora, indexes, and models require separate review. |
| Business examples, knowledge bases, prompts, skills, and evaluation datasets | Individual assets in `application/`, `knowledge/`, `skills/`, and `eval_tasks/` | File-by-file provenance and redistribution rights have not yet been verified. Inclusion in this repository does not automatically grant an MIT license to referenced materials. |

Public accessibility does not by itself authorize relicensing webpages, documents,
images, or derived datasets under MIT. Record sources, versions, original licenses,
and modifications when adding or exporting data. Resolve unclear permissions before release.

## Bundled Site JavaScript

The project site includes Apache ECharts 5.6.0 in
[`site/assets/echarts.min.js`](site/assets/echarts.min.js).
Upstream: [apache/echarts 5.6.0](https://github.com/apache/echarts/tree/5.6.0).

Its complete [LICENSE](licenses/echarts/LICENSE),
[NOTICE](licenses/echarts/NOTICE), and
[d3 BSD 3-Clause license](licenses/echarts/licenses/LICENSE-d3) are retained under
the repository's central `licenses/` directory, preserving the upstream license
directory layout. The NOTICE identifies
`Copyright 2017-2024 The Apache Software Foundation`.

The Pages workflow copies the site and `licenses/echarts/` into the published
artifact, where the latter is served under `licenses/echarts/`. Keep these files
with the JavaScript bundle when deploying the site by another method, and update
them from the matching upstream release when upgrading ECharts.

## Fonts

The [Web stylesheet](web/src/styles/base.css) references these fonts through Google Fonts:

| Font | Upstream license |
| --- | --- |
| Inter | [SIL Open Font License 1.1](https://github.com/rsms/inter/blob/master/LICENSE.txt) |
| Noto Sans SC | [Noto CJK Sans / SIL Open Font License 1.1](https://github.com/notofonts/noto-cjk/blob/main/Sans/LICENSE) |
| JetBrains Mono | [SIL Open Font License 1.1](https://github.com/JetBrains/JetBrainsMono/blob/master/OFL.txt) |

If fonts are self-hosted or bundled, include the copyright notices and full OFL
text for the actual versions distributed and review conditions such as Reserved
Font Names. This project does not grant rights to system fallback fonts.

## Deployment Services and Container Images

The [Runtime deployment](deploy/runtime/docker-compose.yml) and
[Langfuse deployment](deploy/langfuse/docker-compose.yml) reference separate
third-party services. Server source, images, and their embedded components remain
subject to their own version-specific licenses, not the project's MIT License.

| Component | Upstream licensing reference |
| --- | --- |
| Redis Server | [Redis source and license files](https://github.com/redis/redis); Runtime and Langfuse configurations use different major versions and must be reviewed separately |
| MySQL | [MySQL Server](https://github.com/mysql/mysql-server) |
| MinIO | [MinIO](https://github.com/minio/minio); also review the image and its additional components |
| Langfuse | [Langfuse](https://github.com/langfuse/langfuse); different directories or features may carry different licenses |
| ClickHouse | [ClickHouse](https://github.com/ClickHouse/ClickHouse) |
| PostgreSQL | [PostgreSQL license](https://www.postgresql.org/about/licence/) |

Review Python, Node.js, base Linux images, and additional tools against the actual
build artifacts. This section identifies integrations; it does not claim that a
version-specific licensing audit of these services has been completed. Remote
model, search, storage, and other APIs are also subject to provider service terms.

## Distribution and Maintenance

- Update this document when dependencies, upstream revisions, images, or data
  sources change, and inspect the notices in the actual release artifacts.
- Retain required original copyrights, full license texts, NOTICE files, and
  modification notices for copied, modified, or redistributed third-party materials.
- Before release, complete the inventory of transitive and embedded components in
  Web bundles, wheels, and images, and include the required licensing materials.
- An omission from this inventory does not imply KinArc ownership or
  permission to distribute an unlisted third-party component under MIT.
