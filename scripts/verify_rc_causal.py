"""Production acceptance v3. The legacy verify_rc_acceptance entry stays v2."""

from verify_rc_acceptance import main as verify_main


def main():
    return verify_main(evidence_version=3)


if __name__ == "__main__":
    raise SystemExit(main())
