# JR-Foxy backup

This archive contains a consistent SQLite snapshot and the on-disk settings.
manifest.json records checksums, table counts, and the running Docker image.
Source code and Docker images must be obtained separately.

1. Keep the encrypted original and use the owner's existing age private key.
2. Decrypt with: age -d -i <private-key> -o restore.zip <archive.zip.age>
3. Extract into a private directory. Verify every manifest hash, SQLite
   integrity_check and foreign_key_check, and the recorded table counts.
4. Test restoration offline with no real Telegram token or network access.
5. Before production restoration, stop all database writers and preserve
   the current database together with its WAL/SHM files for rollback.
6. Restore data/jrfoxy.db into the correct data directory. Never combine the
   restored database with old WAL/SHM files. Restore config files deliberately.
7. Use the running image/version recorded in the manifest or review required
   migrations before a newer version. Run only one production bot instance.
8. Remove decrypted temporary files after verification/restoration.

This backup does not enable weekly scheduling or Windows downloads.
