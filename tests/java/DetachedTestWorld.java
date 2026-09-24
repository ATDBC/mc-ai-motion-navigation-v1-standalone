import com.mojang.serialization.Lifecycle;
import java.util.List;
import java.util.OptionalLong;
import net.minecraft.block.Block;
import net.minecraft.block.BlockState;
import net.minecraft.client.world.ClientWorld;
import net.minecraft.component.type.MapIdComponent;
import net.minecraft.entity.Entity;
import net.minecraft.entity.damage.DamageType;
import net.minecraft.entity.damage.DamageTypes;
import net.minecraft.entity.player.PlayerEntity;
import net.minecraft.fluid.Fluid;
import net.minecraft.item.map.MapState;
import net.minecraft.recipe.BrewingRecipeRegistry;
import net.minecraft.recipe.RecipeManager;
import net.minecraft.registry.*;
import net.minecraft.registry.entry.RegistryEntry;
import net.minecraft.registry.tag.BlockTags;
import net.minecraft.scoreboard.Scoreboard;
import net.minecraft.sound.SoundCategory;
import net.minecraft.sound.SoundEvent;
import net.minecraft.util.Identifier;
import net.minecraft.util.math.BlockPos;
import net.minecraft.util.math.Vec3d;
import net.minecraft.util.math.intprovider.ConstantIntProvider;
import net.minecraft.util.profiler.DummyProfiler;
import net.minecraft.world.Difficulty;
import net.minecraft.world.World;
import net.minecraft.world.chunk.ChunkManager;
import net.minecraft.world.dimension.DimensionType;
import net.minecraft.world.entity.EntityLookup;
import net.minecraft.world.event.GameEvent;
import net.minecraft.world.tick.QueryableTickScheduler;
import net.minecraft.world.tick.TickManager;

/** No chunks, network, storage or simulation: supplies constructor/scoreboard context only. */
final class DetachedTestWorld extends World {
    private final Scoreboard scoreboard = new Scoreboard();
    final java.util.Map<BlockPos, BlockState> blocks = new java.util.HashMap<>();
    DetachedTestWorld() {
        super(new ClientWorld.Properties(Difficulty.NORMAL, false, true), World.OVERWORLD,
                registries(), RegistryEntry.of(new DimensionType(OptionalLong.empty(), true, false,
                        false, true, 1.0, true, false, -64, 384, 384, BlockTags.INFINIBURN_OVERWORLD,
                        Identifier.of("minecraft:overworld"), 0.0f,
                        new DimensionType.MonsterSettings(false, true, ConstantIntProvider.create(0), 0))),
                () -> DummyProfiler.INSTANCE, true, false, 0L, 0);
    }
    @SuppressWarnings("unchecked")
    private static DynamicRegistryManager registries() {
        // DamageSources requires these keys during World construction. No damage operation is tested.
        SimpleRegistry<DamageType> damage = new SimpleRegistry<>(RegistryKeys.DAMAGE_TYPE, Lifecycle.stable());
        try {
            for (var field : DamageTypes.class.getFields()) {
                if (field.getType() == RegistryKey.class) Registry.register(damage,
                        (RegistryKey<DamageType>) field.get(null), new DamageType(field.getName(), 0.0f));
            }
        } catch (ReflectiveOperationException error) { throw new AssertionError(error); }
        return new DynamicRegistryManager.ImmutableImpl(List.of(damage.freeze()));
    }
    private static AssertionError unsupported() { return new AssertionError("unexpected simulated-world operation"); }
    @Override public Scoreboard getScoreboard() { return scoreboard; }
    @Override public BlockState getBlockState(BlockPos pos) {
        return blocks.getOrDefault(pos, net.minecraft.block.Blocks.AIR.getDefaultState());
    }
    @Override public net.minecraft.resource.featuretoggle.FeatureSet getEnabledFeatures() {
        return net.minecraft.resource.featuretoggle.FeatureFlags.DEFAULT_ENABLED_FEATURES;
    }
    @Override public void updateListeners(BlockPos p, BlockState a, BlockState b, int flags) { throw unsupported(); }
    @Override public void playSound(PlayerEntity p, double x, double y, double z, RegistryEntry<SoundEvent> s, SoundCategory c, float v, float pitch, long seed) { throw unsupported(); }
    @Override public void playSoundFromEntity(PlayerEntity p, Entity e, RegistryEntry<SoundEvent> s, SoundCategory c, float v, float pitch, long seed) { throw unsupported(); }
    @Override public String asString() { return "detached-name-test"; }
    @Override public Entity getEntityById(int id) { throw unsupported(); }
    @Override public TickManager getTickManager() { throw unsupported(); }
    @Override public MapState getMapState(MapIdComponent id) { throw unsupported(); }
    @Override public void putMapState(MapIdComponent id, MapState state) { throw unsupported(); }
    @Override public MapIdComponent increaseAndGetMapId() { throw unsupported(); }
    @Override public void setBlockBreakingInfo(int id, BlockPos p, int progress) { throw unsupported(); }
    @Override public RecipeManager getRecipeManager() { throw unsupported(); }
    @Override protected EntityLookup<Entity> getEntityLookup() { throw unsupported(); }
    @Override public BrewingRecipeRegistry getBrewingRecipeRegistry() { throw unsupported(); }
    @Override public ChunkManager getChunkManager() { throw unsupported(); }
    @Override public RegistryEntry<net.minecraft.world.biome.Biome> getGeneratorStoredBiome(int x, int y, int z) { throw unsupported(); }
    @Override public float getBrightness(net.minecraft.util.math.Direction direction, boolean shaded) { throw unsupported(); }
    @Override public List<? extends PlayerEntity> getPlayers() { throw unsupported(); }
    @Override public QueryableTickScheduler<Block> getBlockTickScheduler() { throw unsupported(); }
    @Override public QueryableTickScheduler<Fluid> getFluidTickScheduler() { throw unsupported(); }
    @Override public void syncWorldEvent(PlayerEntity p, int id, BlockPos pos, int data) { throw unsupported(); }
    @Override public void emitGameEvent(RegistryEntry<GameEvent> event, Vec3d p, GameEvent.Emitter emitter) { throw unsupported(); }
}
