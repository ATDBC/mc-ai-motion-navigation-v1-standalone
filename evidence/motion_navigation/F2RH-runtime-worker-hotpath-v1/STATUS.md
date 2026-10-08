# F2-RH final status

- Source: clean Windows `963d5b49de4b02f42072b3e40426e3f3b8b67b5d`.
- Lifecycle/delivery focused checks: `98/98`.
- Verifier/timing checks: `28/28`.
- Independent review: no open lifecycle P0/P1/P2.
- Five formal non-oracle cases: `5/5`, zero safety events.
- Production P95/P99/max: `10.2680/23.9535/24.9818 ms`.
- Full max: `45.3178 ms`.
- Result: P95 and P99 fail; F2-RH remains open and stopped at RH5.
- The only optimization was same-frame immutable shape-query reuse.
- TP4, oracle, remaining9/42, F2-GP, large suites, D061 and Fabric were not run as stage evidence.
- Repository full check: 1812 tests, 58 failures and 2 errors; shared-scene hash unchanged. These failures remain recorded and are not rewritten as F2-RH successes.
