@echo off
rem ASCII-only wrapper: launched by Windows Task Scheduler so the dev stack lives
rem outside the caller's job object (survives closing the terminal / session).
"C:\Users\EDY\WorkBuddy\testhub_platform\venv\Scripts\python.exe" "C:\Users\EDY\WorkBuddy\testhub_platform\scripts\dev_stack.py" start
