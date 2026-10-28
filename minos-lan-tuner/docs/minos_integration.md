# Minos integration

Pinned checkout: [minos-protocol/minos_subnet](https://github.com/minos-protocol/minos_subnet) commit `4b3c0944b517d09be9b54eb8fa3158ae700b5d5f` (2026-10-08, "Merge pull request #40 from minos-protocol/fix/scoring-cutoff-lead").

`integrations/minos/pin.py` refuses official execution when `git rev-parse HEAD` differs, unless `MINOS_ALLOW_COMMIT_MISMATCH=1`. Dirty files under `templates`, `utils`, `neurons`, `base`, or `configs` also refuse an official study. Practice BAMs and local tuning runs are not part of that check.

The tuner does not submit to live rounds and does not read miner hotkeys.

## Invocation

| Step | Symbol | Source |
| --- | --- | --- |
| GATK | `templates.gatk.variant_call(bam_path, reference_path, output_vcf_path, region, config)` | `templates/gatk.py` |
| Image | `broadinstitute/gatk:4.5.0.0` | quoted literal in `templates/gatk.py` |
| Tool version | `TOOL_VERSIONS["gatk"] == "4.5.0.0"` | `utils/config_loader.py` |
| Flags | `templates.tool_params.validate_and_build_flags("gatk", gatk_options)` | `templates/tool_params.py` |
| Region | `templates.tool_params.validate_region` | `chr1`–`chr22`, `chrX`, `chrY`, `chrM` |
| hap.py | `HappyScorer.score_vcf(truth_vcf, query_vcf, reference_fasta, confident_bed, region, reference_sdf, mutations_vcf)` | `utils/scoring.py` |
| hap.py image | `HAPPY_DOCKER_IMAGE` | `genonet/hap-py@sha256:03acabe84bbfba35f5a7234129d524c563f5657e1f21150a2ea2797f8e6d05f2` |
| bcftools image | `BCFTOOLS_DOCKER_IMAGE` | `quay.io/biocontainers/bcftools:1.20--h8b25389_0` |
| v1 score | `AdvancedScorer.compute_advanced_score(metrics)` | `utils/scoring.py` |
| v2 score | `AdvancedScorer.compute_score_v2(metrics, difficulty_class_counts(parse_happy_vcf(...)))` | same file |
| Names | `utils.scoring_version.scorer_name` | `v1` → `Advanced`, `v2` → `AdvancedV2` |
| Config text | `key=value`, `#` comments, `utils.config_loader._parse_value` | `utils/config_loader.py`, `configs/gatk.conf` |

`variant_call` receives:

```text
config["gatk_options"]   whitelisted quality keys
config["timeout"]        seconds
config["threads"]        docker --cpus and --native-pair-hmm-threads
config["memory_gb"]      docker --memory
config["ref_build"]      "GRCh38" (same literal neurons/miner.py passes; the template does not put it on the GATK command line)
```

The template itself checks `{bam}.bai` and `{reference}.fai`. It mounts the BAM directory and the reference directory read-only and the output directory read-write. It does **not** pass `--network=none` or `--pids-limit`. This tuner does not wrap or rewrite that command, so those two limits are not added.

`neurons/miner.py` `_run_and_score` is the practice/score path this adapter follows: template, then `HappyScorer.score_vcf`, then one of the two AdvancedScorer formulas. Practice mode lists chr18–chr22 samples (`neurons/miner.py` help text). The region validator itself allows a wider contig set. Local `sample.json` files in this checkout have no confident BED. `run_practice` calls `_run_and_score` without `confident_bed`, so the adapter passes `confident_bed=None`.

## Parameters

Accepted names, types, bounds, defaults, and flag strings are `GATK_QUALITY_PARAMS` in `templates/tool_params.py`. The search file `config/search_space.yaml` is checked against that table by `tests/test_contracts.py`.

Stage 1 searches calling confidence, base quality, mapping quality, `min_pruning`, `active_probability_threshold`, and `pcr_indel_model`. Stage 2 adds the assembly-graph and assembly-region keys listed in the YAML. `emit_ref_confidence` is not searched. `full_config` forces it to `NONE` because `GVCF` and `BP_RESOLUTION` change the VCF that hap.py scores.

Every exported `gatk.conf` contains every whitelisted key. Unlisted keys make `validate_and_build_flags` reject the whole config.

## Scores

v1 and v2 are different scales. `score_metrics` calls only the requested formula. A missing hap.py VCF leaves the v2 score null. The v1 number is not substituted.

`combined_final` is `official_score / 100`, matching `neurons/miner.py` and `neurons/validator.py`.

A validator records a score only when `_valid_round_score(..., allow_zero=True)` accepts `combined_final` and `_is_zero_input_advanced_fingerprint` is false (`neurons/validator.py`). The adapter copies those two conditions in `recording_decision`. A dropped number is stored as `discarded_official_score` on a failed job. It is not an Optuna value. A crash is `FAIL` or `PRUNED`, never a synthetic zero.

The active network `scoring_version` is published by the platform (`utils/scoring_version.py`). This offline process does not call `resolve(None)`, because that falls back to a remembered version or to v1. The operator passes `--scoring-version v1` or `v2`. A dataset is imported for one version, and a study must use that same version.

## Cache identity

A successful evaluation is reused only when all of these match: config SHA-256, dataset manifest SHA-256, reference SHA-256, Minos commit, scoring version, GATK image name and id, hap.py image, bcftools image, thread count, memory, and `mock` versus `official`. Mock numbers never occupy an official cache row.

## Dataset files

Practice import reads `sample.json` keys `bam`, `bai`, `reference`, `reference_index`, `truth`, `truth_index`, `mutations`, `mutations_index`, `sdf`, plus a `.dict` beside the FASTA when that file exists. The SDF directory is expanded to one hashed file per member. A FASTA header that matches `GRCh37` or `hg19` is rejected. The miner still passes `ref_build=GRCh38` for these chromosome FASTAs; the manifest records the raw header (often `>chr21`) separately.

## One official run on this host

`MINOS_REAL=1` with scoring version **v1** (chosen explicitly; the platform was not asked), sample `datasets/practice/round_d17838295b8cfe54/sample.json`, region `chr21:19858319-21637960`, 4 threads, 8 GB. The coordinator stored `official_score=95.32677496066316` from `AdvancedScorer.compute_advanced_score`. GATK image id `sha256:8a29403b528a417cd1630c33affe82726019788e24e4dfe00e2586c6b4195e9f`. That is a single default-config evaluation, not a tuning result.

## Unknown

- The live platform's current `scoring_version` was not queried.
- Whether every practice sample on the platform includes a confident BED is unknown for samples that are not in this checkout. The local files do not.
- Native Windows execution of `templates/gatk.py` is not claimed. The template shells out to Docker. On Windows that means Docker Desktop Linux containers. See `docs/windows.md`.
- GATK can suggest `min_assembly_region_size` above `max_assembly_region_size` because the official validator does not compare those two keys. That failure is a failed trial, not a zero score.
