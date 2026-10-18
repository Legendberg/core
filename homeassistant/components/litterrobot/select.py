"""Support for Litter-Robot selects."""

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, replace
from typing import Any, Generic, TypeVar, override

from pylitterbot import FeederRobot, LitterRobot, LitterRobot4, LitterRobot5, Robot
from pylitterbot.robot.litterrobot4 import BrightnessLevel, NightLightMode

from homeassistant.components.select import (
    DOMAIN as SELECT_DOMAIN,
    SelectEntity,
    SelectEntityDescription,
)
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er, issue_registry as ir
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .coordinator import LitterRobotConfigEntry, LitterRobotDataUpdateCoordinator
from .entity import LitterRobotEntity, _WhiskerEntityT, whisker_command

PARALLEL_UPDATES = 1

_CastTypeT = TypeVar("_CastTypeT", int, float, str)

# Version in which the deprecated Litter-Robot 5 globe brightness select goes.
DEPRECATED_REMOVAL_VERSION = "2027.5.0"


@dataclass(frozen=True, kw_only=True)
class RobotSelectEntityDescription(
    SelectEntityDescription,
    Generic[_WhiskerEntityT, _CastTypeT],  # noqa: UP046
):
    """A class that describes robot select entities."""

    entity_category: EntityCategory = EntityCategory.CONFIG
    # Replaced by another entity: existing users keep it for now, new ones don't
    # get it. See _async_check_deprecated.
    deprecated: bool = False
    current_fn: Callable[[_WhiskerEntityT], _CastTypeT | None]
    options_fn: Callable[[_WhiskerEntityT], list[_CastTypeT]]
    select_fn: Callable[[_WhiskerEntityT, str], Coroutine[Any, Any, bool]]


GLOBE_BRIGHTNESS = RobotSelectEntityDescription[LitterRobot4 | LitterRobot5, str](
    key="globe_brightness",
    translation_key="globe_brightness",
    current_fn=(
        lambda robot: (
            bri.name.lower() if (bri := robot.night_light_level) is not None else None
        )
    ),
    options_fn=lambda _: [level.name.lower() for level in BrightnessLevel],
    select_fn=(
        lambda robot, opt: robot.set_night_light_brightness(
            BrightnessLevel[opt.upper()]
        )
    ),
)

ROBOT_SELECT_MAP: dict[
    type[Robot] | tuple[type[Robot], ...], tuple[RobotSelectEntityDescription, ...]
] = {
    LitterRobot: (
        RobotSelectEntityDescription[LitterRobot, int](
            key="cycle_delay",
            translation_key="cycle_delay",
            unit_of_measurement=UnitOfTime.MINUTES,
            current_fn=lambda robot: robot.clean_cycle_wait_time_minutes,
            options_fn=lambda robot: robot.VALID_WAIT_TIMES,
            select_fn=lambda robot, opt: robot.set_wait_time(int(opt)),
        ),
    ),
    LitterRobot4: (GLOBE_BRIGHTNESS,),
    # The Litter-Robot 5 night light entity sets brightness continuously, which
    # makes this three-level select redundant.
    LitterRobot5: (replace(GLOBE_BRIGHTNESS, deprecated=True),),
    (LitterRobot4, LitterRobot5): (
        RobotSelectEntityDescription[LitterRobot4 | LitterRobot5, str](
            key="globe_light",
            translation_key="globe_light",
            current_fn=(
                lambda robot: (
                    mode.name.lower()
                    if (mode := robot.night_light_mode) is not None
                    else None
                )
            ),
            options_fn=lambda _: [mode.name.lower() for mode in NightLightMode],
            select_fn=(
                lambda robot, opt: robot.set_night_light_mode(
                    NightLightMode[opt.upper()]
                )
            ),
        ),
        RobotSelectEntityDescription[LitterRobot4 | LitterRobot5, str](
            key="panel_brightness",
            translation_key="brightness_level",
            current_fn=(
                lambda robot: (
                    bri.name.lower()
                    if (bri := robot.panel_brightness) is not None
                    else None
                )
            ),
            options_fn=lambda _: [level.name.lower() for level in BrightnessLevel],
            select_fn=(
                lambda robot, opt: robot.set_panel_brightness(
                    BrightnessLevel[opt.upper()]
                )
            ),
        ),
    ),
    FeederRobot: (
        RobotSelectEntityDescription[FeederRobot, float](
            key="meal_insert_size",
            translation_key="meal_insert_size",
            unit_of_measurement="cups",
            current_fn=lambda robot: robot.meal_insert_size,
            options_fn=lambda robot: robot.VALID_MEAL_INSERT_SIZES,
            select_fn=lambda robot, opt: robot.set_meal_insert_size(float(opt)),
        ),
    ),
}


@callback
def _async_check_deprecated(
    hass: HomeAssistant, robot: Robot, description: RobotSelectEntityDescription
) -> bool:
    """Return whether a select should be created, handling deprecated ones.

    A deprecated select is only kept for users who already have it. It is decided
    from the entity registry alone, not from whether automations use it: those
    integrations may not have finished loading when this runs, so asking them at
    startup can wrongly report an entity as unused.

    - not registered: never create it, so new installs don't get it
    - registered and disabled: the user opted out, so remove it
    - registered and enabled: keep it and raise a repair issue
    """
    if not description.deprecated:
        return True

    entity_registry = er.async_get(hass)
    issue_id = f"deprecated_{description.key}_{robot.serial}"
    entity_id = entity_registry.async_get_entity_id(
        SELECT_DOMAIN, DOMAIN, f"{robot.serial}-{description.key}"
    )
    if entity_id is None:
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return False

    entity_entry = entity_registry.async_get(entity_id)
    if entity_entry is None or entity_entry.disabled:
        entity_registry.async_remove(entity_id)
        ir.async_delete_issue(hass, DOMAIN, issue_id)
        return False

    ir.async_create_issue(
        hass,
        DOMAIN,
        issue_id,
        breaks_in_ha_version=DEPRECATED_REMOVAL_VERSION,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=f"deprecated_{description.key}",
        translation_placeholders={"entity_id": entity_id},
    )
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: LitterRobotConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Litter-Robot selects using config entry."""
    coordinator = entry.runtime_data
    known_robots: set[str] = set()

    def _check_robots() -> None:
        all_robots = coordinator.account.robots
        current_robots = {robot.serial for robot in all_robots}
        new_robots = current_robots - known_robots
        if new_robots:
            known_robots.update(new_robots)
            async_add_entities(
                LitterRobotSelectEntity(
                    robot=robot, coordinator=coordinator, description=description
                )
                for robot in all_robots
                if robot.serial in new_robots
                for robot_type, descriptions in ROBOT_SELECT_MAP.items()
                if isinstance(robot, robot_type)
                for description in descriptions
                if _async_check_deprecated(hass, robot, description)
            )

    _check_robots()
    entry.async_on_unload(coordinator.async_add_listener(_check_robots))


class LitterRobotSelectEntity(
    LitterRobotEntity[_WhiskerEntityT],
    SelectEntity,
    Generic[_WhiskerEntityT, _CastTypeT],  # noqa: UP046
):
    """Litter-Robot Select."""

    entity_description: RobotSelectEntityDescription[_WhiskerEntityT, _CastTypeT]

    def __init__(
        self,
        robot: _WhiskerEntityT,
        coordinator: LitterRobotDataUpdateCoordinator,
        description: RobotSelectEntityDescription[_WhiskerEntityT, _CastTypeT],
    ) -> None:
        """Initialize a Litter-Robot select entity."""
        super().__init__(robot, coordinator, description)
        options = self.entity_description.options_fn(self.robot)
        self._attr_options = list(map(str, options))

    @property
    @override
    def current_option(self) -> str | None:
        """Return the selected entity option to represent the entity state."""
        option = self.entity_description.current_fn(self.robot)
        return None if option is None else str(option)

    @whisker_command
    @override
    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""
        await self.entity_description.select_fn(self.robot, option)
