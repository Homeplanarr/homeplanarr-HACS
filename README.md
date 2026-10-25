# Homeplanarr for Home Assistant

Connects Home Assistant to your [Homeplanarr](https://homeplanarr.com) household.
It replaces the hand-written `utility_meter`, `rest_command` and automation setup
from the help page.

**Home Assistant → Homeplanarr**

- **Electricity:** pick your energy meters (kWh, total). Every quarter hour (or
  hour) the integration sends what the house used in the finished period, stamped
  with the period's start, so Homeplanarr prices it against the right spot-price
  hour. It sums the meters itself: no Sum helper or utility meter needed.
- **Water and solar:** total meters, sent as use or production per hour.
- **Indoor sensors:** temperature, humidity, CO2, radon and similar, once an
  hour. The entity's area is matched to the Homeplanarr room with the same name.
- **Batteries:** smoke and leak detectors and other battery sensors, shown under
  Integrations with a low-battery warning.
- **NFC tags (optional):** tag scans are forwarded, and tags registered in
  Homeplanarr complete their chore.

**Homeplanarr → Home Assistant**

Sensors for monthly cost, electricity, water and solar this month, chores due and
overdue, next chore, the month's chore leader, the next contract renewal, the
start of the cheapest three hours, devices with low battery and silent sensors.

**Shopping list:** the household's shopping list is a to-do list in Home
Assistant (`todo.homeplanarr_shopping_list`), synced both ways. Adding, renaming,
ticking off or removing an item in HA changes it in Homeplanarr straight away,
and changes made in the app show up in HA within a minute. The quantity is the
item's description.

Already have a to-do list you use for shopping (HA's Shopping list, Local To-do,
Google Keep...)? Pick it under *Configure → Sync the shopping list with an
existing list*. Homeplanarr then keeps that list in step instead of adding its
own: items with the same name are paired rather than doubled, items only on your
list are added to Homeplanarr, and when both sides changed the same item since
the last sync, Homeplanarr's version wins. Lists without descriptions (like HA's
Shopping list) keep the quantity in Homeplanarr only. Clear the field to go back
to `todo.homeplanarr_shopping_list`.

**Car odometers:** if your car is in Home Assistant (Volvo, Tesla, Kia and so on),
pick its odometer sensor under *Car odometers*, then the Homeplanarr vehicle it
belongs to. The reading goes to the vehicle's log once a day, converted to the
vehicle's unit (km or Swedish mil). Add the vehicle under *Vehicles* in
Homeplanarr first.

**Action** `homeplanarr.complete_chore` marks a chore as done for a member, by
name or id, for buttons, voice assistants and automations.

## Install

1. In HACS: *⋮ → Custom repositories*, add
   `https://github.com/Homeplanarr/homeplanarr-HACS` with type *Integration*, then download **Homeplanarr** and restart Home Assistant.
2. In Homeplanarr: *Household → API access*, create a key with **full** access.
3. In Home Assistant: *Settings → Devices & services → Add integration →
   Homeplanarr*. Enter the address (`https://homeplanarr.com` unless you run your
   own) and the key, then pick what to send. *Configure* changes it later.

## Good to know

- If you used the manual setup before, remove its automation and
  `rest_command`, or electricity is counted twice. The same goes for Tibber:
  send electricity from one place only.
- Times are sent in Home Assistant's time zone. It should be the same as the
  household's time zone in Homeplanarr.
- Readings that could not be sent (no internet, Homeplanarr down) are kept and
  sent with the next push, up to a day of them. If Home Assistant itself was
  off, the use meanwhile is spread evenly over the missed periods (up to 24
  hours; a longer gap is skipped).
- A meter that goes down (reset, or a member of a sum missing) starts over
  from its new value without counting the drop.

## Example: chore button

```yaml
- alias: "Trash taken out"
  triggers:
    - trigger: state
      entity_id: input_button.trash
  actions:
    - action: homeplanarr.complete_chore
      data:
        chore: "Take out trash"
        member: "Alice"
```

## Example: dishwasher in the cheapest hours

```yaml
- alias: "Dishwasher in the cheapest hours"
  triggers:
    - trigger: time
      at: sensor.homeplanarr_cheapest_3_hours_start
  actions:
    - action: switch.turn_on
      target:
        entity_id: switch.dishwasher
```

## Releasing

Bump `version` in `custom_components/homeplanarr/manifest.json` and merge. The
Release workflow then publishes the GitHub release `vX.Y.Z`, which is what HACS
offers as an update. A version that is already released is skipped.
