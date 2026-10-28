"""GATK's failure text must be the exception, not the startup banner."""

from templates.gatk import failure_excerpt


def test_failure_excerpt_keeps_the_exception_between_banner_and_command() -> None:
    stderr = "\n".join(
        [
            "INFO  NativeLibraryLoader - Loading libgkl_compression.so",
            "INFO  HaplotypeCaller - The Genome Analysis Toolkit (GATK) v4.5.0.0",
            "java.lang.IllegalStateException: Graph must have ref source and sink vertices",
            "\tat org.broadinstitute.hellbender.tools.walkers.haplotypecaller.graphs.BaseGraph.removePathsNotConnectedToRef(BaseGraph.java:501)",
            "Using GATK jar /gatk/gatk-package-4.5.0.0-local.jar",
            "Running:",
            "    java -jar /gatk/gatk-package-4.5.0.0-local.jar HaplotypeCaller --min-dangling-branch-length 17",
        ]
    )
    message = failure_excerpt(stderr, 3)
    assert message == "rc=3 java.lang.IllegalStateException: Graph must have ref source and sink vertices"
    assert "NativeLibraryLoader" not in message
