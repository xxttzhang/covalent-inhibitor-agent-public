# Security

Never commit provider tokens, SSH keys, approval signing keys, runtime databases, commercial licenses, or research exports containing confidential information.

Report suspected exposure through GitHub's private vulnerability reporting or another established private channel to the repository owner. Do not include secrets, private research data or exploit details in public issues or logs. If private vulnerability reporting is not enabled, contact the owner privately before disclosing details.

The bounded source check detects selected token formats, forbidden public paths and prohibited artifacts. It is not proof that all secrets have been removed. This public candidate starts with a fresh Git history so private history is not transferred.
