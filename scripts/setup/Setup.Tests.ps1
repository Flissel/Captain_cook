BeforeAll {
    Import-Module "$PSScriptRoot/Common.psm1" -Force
}

Describe 'Common setup contracts' {
    It 'creates a stable result object' {
        $result = New-SetupResult -Component 'Python' -Status 'Missing' -Message 'Python 3.11 fehlt' -Remediation 'Install'

        $result.PSObject.Properties.Name | Should -Be @('Component', 'Status', 'Message', 'Remediation', 'Data')
        $result.Status | Should -Be 'Missing'
    }

    It 'redacts every supplied secret before writing a log' {
        $path = Join-Path $TestDrive 'setup.log'

        Write-SetupLog -Path $path -Message 'token=abc password=xyz' -Secrets @('abc', 'xyz')

        (Get-Content $path -Raw).Trim() | Should -Match 'token=\*\*\* password=\*\*\*$'
    }

    It 'round-trips a non-secret checkpoint' {
        $path = Join-Path $TestDrive 'checkpoint.json'

        Save-SetupCheckpoint -Path $path -Stages @{ Preflight = 'Complete' }

        (Get-SetupCheckpoint -Path $path).Preflight | Should -Be 'Complete'
    }
}
