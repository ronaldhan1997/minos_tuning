"""Where a worker places downloaded files so the official template can see them.

``templates.gatk.variant_call`` looks for ``{bam}.bai`` and ``{reference}.fai``
beside those files. Index roles are renamed to that layout on the way in.
"""

from __future__ import annotations

from pathlib import Path


def cache_relative(
    role: str,
    source_name: str,
    *,
    bam_name: str | None = None,
    reference_name: str | None = None,
    truth_name: str | None = None,
    mutations_name: str | None = None,
    sdf_relative: str | None = None,
) -> str:
    if role == "bam":
        return f"reads/{source_name}"
    if role == "bai":
        if not bam_name:
            raise ValueError("bai placement needs the BAM file name")
        return f"reads/{bam_name}.bai"
    if role == "reference":
        return f"reference/{source_name}"
    if role == "reference_index":
        if not reference_name:
            raise ValueError("reference index placement needs the FASTA file name")
        return f"reference/{reference_name}.fai"
    if role == "reference_dict":
        if not reference_name:
            raise ValueError("reference dictionary placement needs the FASTA file name")
        # GATK looks beside the FASTA for the suffix-replaced name: chr21.fa -> chr21.dict.
        dictionary = Path(reference_name).with_suffix(".dict").name
        return f"reference/{dictionary}"
    if role == "truth":
        return f"truth/{source_name}"
    if role == "truth_index":
        if not truth_name:
            raise ValueError("truth index placement needs the truth VCF file name")
        return f"truth/{truth_name}.tbi"
    if role == "mutations":
        return f"mutations/{source_name}"
    if role == "mutations_index":
        if not mutations_name:
            raise ValueError("mutations index placement needs the mutations VCF file name")
        return f"mutations/{mutations_name}.tbi"
    if role == "sdf":
        if sdf_relative is None or sdf_relative == "":
            raise ValueError("sdf placement needs a relative path inside the SDF directory")
        relative = Path(sdf_relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"unsafe sdf relative path: {sdf_relative}")
        return f"sdf/{relative.as_posix()}"
    raise ValueError(f"unknown dataset role: {role}")


REQUIRED_ROLES = (
    "bam",
    "bai",
    "reference",
    "reference_index",
    "truth",
    "truth_index",
    "mutations",
    "mutations_index",
    "sdf",
)
