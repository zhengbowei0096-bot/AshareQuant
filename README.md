# AshareQuant-Private

Private source-code snapshot of an A-share research and portfolio analysis project.

This repository deliberately excludes live API credentials, certificates, account settings, holdings, watchlists, market datasets, generated reports, logs, backups, local automation settings, and earlier experiment repositories. The original working project is unchanged.

## Local setup

Install the dependencies from requirements.txt (optional model dependency lock files are included). Copy config.example.json to config.json and configure your own research settings. Supply your own watchlist.csv with symbol,name,enabled columns. Configure any additional model settings locally. Provide TUSHARE_TOKEN through your local environment, or the application's private data/credentials/tushare.token file with mode 0600. Never commit it.

This is a source snapshot, not a copy of the live trading environment. Missing local datasets and account configuration are intentional. Tests use synthetic examples; some require additional local configuration or optional dependencies. No account connection or automated job is installed by cloning this repository.

## History and privacy

The original project root had no Git repository. Two nested experiment repositories each had one commit and were reviewed for known credentials and private-key patterns; they also contained configuration and watchlist files. Neither repository nor its history is included here. This repository starts with a new initial commit.

Account-like numerical fixtures in tests have been replaced with fictional examples. No scanner can prove that every possible secret is absent; review future commits before pushing. Private visibility does not replace credential hygiene.
