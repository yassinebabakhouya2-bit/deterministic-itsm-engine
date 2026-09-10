# Demo knowledge bases (100% synthetic data)

These records are **entirely fictional** — no real client data. They are used to
demonstrate and test KnowledgeEngine v9 (multi-tenant isolation, evaluation, RAG).

| Client | Folder | Systems (unique names) |
|---|---|---|
| Client A | `kb/clienta/` | Aurora ERP, SafePass VPN, DeskFlow, GateKey, VaultDrive, PrintHub A |
| Client B | `kb/clientb/` | Helios CRM, NimbusVPN, TicketWave, BioBadge, CloudStore B, ScanPro |
| Client C | `kb/clientc/` | Titan ERP, ZephyrVPN, FlowDesk, KeyCard C, DataNest, MegaPrint |

**"Isolation canary" design:** each client has systems with **unique** names.
If one client's record shows up in another client's index, the leak is immediately
detectable — this is the basis of the cross-tenant isolation proof (Milestone 2).

Each folder is uploaded to its `kb-<client>` Blob container (metadata `clientid`),
then indexed into its dedicated `idx-<client>` index. Associated golden datasets: `eval/golden_<client>.jsonl`.
