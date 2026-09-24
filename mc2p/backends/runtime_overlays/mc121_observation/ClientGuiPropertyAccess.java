package com.mc2p.observation;

import java.util.List;
import net.minecraft.screen.Property;

/** Read-only collector bridge to the current handler's normal client-synchronized properties. */
public interface ClientGuiPropertyAccess {
    List<Property> mc2p$properties();
}
