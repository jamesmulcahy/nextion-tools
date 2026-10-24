"""What names exist on the display, and what they hold before anything is assigned, from its HMI.

How the display resolves a name:

- `comp.attr`, without a page, means a component on the current page.
- `page.comp.attr` reaches a component on another page only if it is global (vscope). A local
  component of a page that is not shown does not exist as far as the display is concerned. That
  is why the firmware's unprefixed screensaver forecast labels fail with 0x1A everywhere but on
  the screensaver.
- The attribute must be one the component has.
- A name without a dot is a system variable.

Display hands this already-qualified keys (`page.comp.attr`), so the first rule is applied
before a key gets here.
"""

from dataclasses import dataclass
from typing import Optional

from ..hmi import Component, Page, Project, Value

# System variables from the Nextion Instruction Set, with what a display holds at power-on where
# that matters. dp (the current page) is answered live.
SYSTEM_VARIABLES = {
    "dp": None, "dim": 100, "dims": 100, "baud": 115200, "bauds": 115200, "spax": 0, "spay": 0,
    "thc": 0, "thdra": 0, "ussp": 0, "thsp": 0, "thup": 0, "usup": 0, "wup": 255, "sleep": 0,
    "bkcmd": 2, "delay": 0, "rand": 0, "crcval": 0, "sendxy": 0, "recmod": 0, "sys0": 0, "sys1": 0,
    "sys2": 0, "tch0": 0, "tch1": 0, "tch2": 0, "tch3": 0, "volume": 100, "lowpower": 0, "addr": 0,
    "pwmf": 50, "audio0": 0, "audio1": 0, "eql": 0, "eqm": 0, "eqh": 0,
}
SYSTEM_VARIABLES.update({"rtc%d" % i: 0 for i in range(7)})
SYSTEM_VARIABLES.update({"pio%d" % i: 0 for i in range(8)})
SYSTEM_VARIABLES.update({"pwm%d" % i: 50 for i in range(4, 8)})
SYSTEM_VARIABLES.update({"eq%d" % i: 7 for i in range(10)})

# Not attributes but instructions, checked against the component alone.
COMPONENT_OPERATIONS = frozenset({"vis"})


@dataclass
class Target:
    page: Optional[Page]  # None for a system variable
    component: Optional[Component]
    attr: str

    @property
    def default(self) -> Optional[Value]:
        if self.component is None:
            return SYSTEM_VARIABLES.get(self.attr)
        return self.component.attrs.get(self.attr)


class Screen:
    def __init__(self, hmi: Project):
        self.hmi = hmi

    @property
    def first_page(self) -> Optional[str]:
        """The page a display shows when it starts: page 0."""
        return self.hmi.pages[0].name if self.hmi.pages else None

    def page(self, name_or_id: str) -> Optional[Page]:
        name_or_id = name_or_id.strip()
        if name_or_id.isdigit():
            page_id = int(name_or_id)
            return self.hmi.pages[page_id] if page_id < len(self.hmi.pages) else None
        return self.hmi.find_page(name_or_id)

    def resolve(self, key: str, current_page: Optional[str]) -> Optional[Target]:
        """The target of an already-qualified key, or None if the display has no such name."""
        parts = key.split(".")
        if len(parts) == 1:
            return Target(None, None, key) if key in SYSTEM_VARIABLES else None
        if len(parts) != 3:
            return None
        page_name, comp_name, attr = parts
        page = self.hmi.find_page(page_name)
        if page is None:
            return None
        comp = page.find(comp_name)
        if comp is None:
            return None
        if page_name != current_page and not comp.is_global:
            return None
        if attr not in comp.attrs and attr not in COMPONENT_OPERATIONS:
            return None
        return Target(page, comp, attr)

    def local_keys_prefix(self, page_name: str):
        """Key prefixes of a page's local components, which a page load resets."""
        page = self.hmi.find_page(page_name)
        if page is None:
            return []
        return ["%s.%s." % (page_name, c.name) for c in page.components if not c.is_global]
