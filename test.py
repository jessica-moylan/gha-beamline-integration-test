from enum import Enum

class ENDSTATION_ENUM(Enum):
    SIX = "six"
    Keithley = "keithley"


print("Please select an endstation from:")
for e in ENDSTATION_ENUM:
    print(f"\t- {e.name}")
endstation_choice = input("Enter your selection: ")
try:
    endstation = ENDSTATION_ENUM[endstation_choice]
except KeyError as e:
    raise Exception(
        f"Endstation choice '{endstation_choice}' is not one of the valid options."
    ) from e
print(f"You selected: {endstation.name} ({endstation.value})")