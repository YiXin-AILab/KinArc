# Third-party license texts

This directory is the central source location for third-party license texts,
copyright notices, and upstream attribution files retained by this project.
The project's own license remains at [`../LICENSE`](../LICENSE); the scope and
source inventory are documented in [`../THIRD_PARTY_NOTICES.md`](../THIRD_PARTY_NOTICES.md).

| Component | Retained files | Source version |
| --- | --- | --- |
| Apache ECharts, including its bundled d3 components | [LICENSE](echarts/LICENSE), [NOTICE](echarts/NOTICE), [d3 license](echarts/licenses/LICENSE-d3) | ECharts 5.6.0 distribution |
| EvoAgentBench benchmark implementation | [LICENSE](evoagentbench/LICENSE), [third-party notices](evoagentbench/THIRD_PARTY_NOTICES.md) | `948a17288782d5120778da16b4cf1cad9305d8b4`, `benchmark/` |
| Tau2 / τ³-bench | [LICENSE](tau2-bench/LICENSE) | `v1.0.1` |

Add notices for other redistributed third-party components here as they are
identified. Preserve the upstream text and any relative license-file references;
record the component version and its scope in the root third-party notices.
Retaining a code license does not grant additional rights to independent data,
attachments, models, or services.

Distribution artifacts must include the notices for the components they actually
contain. The Pages workflow stages ECharts notices from this directory into the
site artifact; they are not separately maintained under `site/`.
Do not remove license files from separately installed dependencies or upstream
checkouts when adding a central copy here.
