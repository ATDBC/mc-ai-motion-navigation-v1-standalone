package com.kyhsgeekcode.minecraftenv.mixin;

import com.mc2p.observation.ClientGuiPropertyAccess;
import java.util.List;
import net.minecraft.screen.Property;
import net.minecraft.screen.ScreenHandler;
import org.spongepowered.asm.mixin.Mixin;
import org.spongepowered.asm.mixin.gen.Accessor;

@Mixin(ScreenHandler.class)
public interface ScreenHandlerPropertiesMixin extends ClientGuiPropertyAccess {
    @Accessor("properties")
    List<Property> mc2p$properties();
}
