name: Govt Site Monitor

on:
  schedule:
    - cron: "*/5 * * * *"    # every 5 minutes
  workflow_dispatch: {}       # allows manual "Run workflow" button too

permissions:
  contents: write

# Prevents two runs from overlapping and fighting over the git push if one
# run happens to take longer than 5 minutes.
concurrency:
  group: govt-site-monitor
  cancel-in-progress: false

jobs:
  check:
    runs-on: ubuntu-latest
    timeout-minutes: 12
    steps:
      - name: Checkout repo
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: "3.11"

      - name: Install dependencies
        run: pip install -r requirements.txt

      - name: Install Playwright browser
        run: python -m playwright install --with-deps chromium

      - name: Run monitor
        env:
          TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
          TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
        run: python monitor.py

      - name: Commit updated state
        run: |
          git config user.name "govt-site-monitor-bot"
          git config user.email "actions@github.com"
          git add state.json
          git diff --cached --quiet || git commit -m "update state [skip ci]"
          git pull --rebase
          git push
