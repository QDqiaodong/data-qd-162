# Dimensional parcel rating v1

Each shipment is rated independently against an explicit service/zone tariff. Dimensions are positive integer millimetres; actual weight is positive integer grams. Volume in cubic centimetres is length*width*height / 1,000. Dimensional weight in grams is volume_cm3 / divisor_cm3_per_kg * 1,000, equivalently length*width*height / divisor_cm3_per_kg. Chargeable weight is the greater of actual and dimensional weight, rounded upward to a whole step_grams. Girth is length+2*(width+height); excess parcels are reported oversize and are not charged.

Tariff amounts are integer cents: base, per chargeable step, optional remote-zone fee, fuel basis points on that subtotal, and insurance basis points on declared value above an included threshold. Basis-point charges use exact nonnegative half-up rounding. All totals are integer cents. A tariff is unique by service and zone; every shipment must match one.

The fixture rates a synthetic parcel with dimensional weight above actual weight and reports each charge component. No live carrier API, taxes, discounts, currency conversion, dimensional packaging optimization or delivery promise is included.
