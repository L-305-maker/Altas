"""Run the Living Guideline reference application offline evaluation."""

import asyncio

from applications.living_guideline.evaluation import main

if __name__ == "__main__":
    asyncio.run(main())
