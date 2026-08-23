# Station and Vehicle import CSV v1

Use **Import Stations and Vehicles** to stage a Department-scoped CSV for review. Uploading and review decisions do not change canonical records; **Apply accepted changes** is the only canonical mutation step.

## Columns

`row_type` is required and is either `station` or `vehicle`.

| Column | Station row | Vehicle row |
| --- | --- | --- |
| `station_short_code` | required | preferred Station reference |
| `station_name` | required | optional full Station-name reference |
| `street`, `house_number`, `postal_code`, `city` | optional canonical address fields | leave blank |
| `vehicle_name` | leave blank | required |
| `vehicle_call_sign`, `vehicle_asset_identifier` | leave blank | optional |

Vehicle Station references resolve in the importing Department by Short Code first, or by full Station name when no Short Code is supplied. Multiple matches require an explicit review choice. A missing Station is staged for reviewer confirmation and is only created with the dependent Vehicle during final Apply. A Station row in the same CSV can satisfy a later Vehicle reference.

Blank or absent Vehicle rows never retire, transfer, unprovision, or otherwise change existing Vehicles or Tablets.

```csv
row_type,station_short_code,station_name,street,house_number,postal_code,city,vehicle_name,vehicle_call_sign,vehicle_asset_identifier
station,F25,Station 25,Musterstraße,12,22041,Hamburg,,,
vehicle,F25,,,,,,HLF 1,Florian 25/46-1,HH-F25-01
```
