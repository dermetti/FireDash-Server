# Permanent beta migration dependency graph

Sixteen ordinary FireDash migrations. Dependencies are unchanged from accepted Phase 1. `AUTH_USER_MODEL` resolves to the accounts root through Django swappable dependencies. No replacement declarations remain.

| Permanent migration | Declared dependencies |
| --- | --- |
| `accounts.0001_squashed_0002_beta` | `auth.0012_alter_user_first_name_max_length` |
| `assignments.0001_squashed_0002_beta` | `organizations.0001_squashed_0007_beta`, `personnel.0001_squashed_0002_beta`, `tablets.0001_squashed_0002_beta`, `AUTH_USER_MODEL` |
| `audit.0001_squashed_0004_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `authorization.0001_squashed_0007_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `file_library.0001_squashed_0010_beta` | `audit.0001_squashed_0004_beta`, `organizations.0001_squashed_0007_beta`, `publications.0012_squashed_0030_beta`, `AUTH_USER_MODEL` |
| `ingestion.0001_squashed_0006_beta` | `audit.0001_squashed_0004_beta`, `file_library.0001_squashed_0010_beta`, `organizations.0001_squashed_0007_beta`, `publications.0012_squashed_0030_beta`, `AUTH_USER_MODEL` |
| `organizations.0001_squashed_0007_beta` | `AUTH_USER_MODEL` |
| `outbound_mail.0001_squashed_0006_beta` | `organizations.0001_squashed_0007_beta`, `tablets.0003_squashed_0009_beta`, `AUTH_USER_MODEL` |
| `personnel.0001_squashed_0002_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `publications.0001_squashed_0011_beta` | `organizations.0001_squashed_0007_beta`, `tablets.0001_squashed_0002_beta`, `AUTH_USER_MODEL` |
| `publications.0012_squashed_0030_beta` | `audit.0001_squashed_0004_beta`, `publications.0001_squashed_0011_beta`, `reference_data.0001_squashed_0015_beta`, `tablets.0003_squashed_0009_beta` |
| `publications.0031_squashed_0033_beta` | `file_library.0001_squashed_0010_beta`, `publications.0012_squashed_0030_beta` |
| `reference_data.0001_squashed_0015_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `report_transfer.0001_squashed_0003_beta` | `organizations.0001_squashed_0007_beta`, `personnel.0001_squashed_0002_beta`, `tablets.0003_squashed_0009_beta` |
| `tablets.0001_squashed_0002_beta` | `organizations.0001_squashed_0007_beta`, `AUTH_USER_MODEL` |
| `tablets.0003_squashed_0009_beta` | `assignments.0001_squashed_0002_beta`, `organizations.0001_squashed_0007_beta`, `publications.0001_squashed_0011_beta`, `tablets.0001_squashed_0002_beta`, `AUTH_USER_MODEL` |

The ordering that requires multiple stages remains: early tablets -> first publications -> later tablets -> middle publications -> file library -> final publications. Assignments depends on early tablets; reference data and audit precede middle publications. The final publication stage follows file-library publication/capacity structures. Keeping these boundaries avoids a publications/tablets cycle and preserves cross-app SQL type availability.

A fresh database executes these 16 nodes. An accepted Phase 1 database already has these same node names recorded and executes none. Historical recorder rows remain untouched.
