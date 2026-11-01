[IO.File]::WriteAllText($env:BACKUP_TEST_CALL, (ConvertTo-Json -InputObject @($args)))
exit ([int]$env:BACKUP_TEST_EXIT)
