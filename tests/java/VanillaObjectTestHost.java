import java.io.IOException;
import java.net.URL;
import java.net.URLClassLoader;
import java.nio.file.Path;
import java.util.Arrays;
import net.fabricmc.loader.impl.transformer.PackageAccessFixer;
import org.objectweb.asm.ClassReader;
import org.objectweb.asm.ClassWriter;
import org.objectweb.asm.Opcodes;

/** Test-only host for named vanilla jars: apply Fabric's normal package access fix in memory. */
public class VanillaObjectTestHost extends URLClassLoader {
    private VanillaObjectTestHost(URL[] urls) { super(urls, ClassLoader.getPlatformClassLoader()); }

    @Override
    protected Class<?> findClass(String name) throws ClassNotFoundException {
        if (!name.startsWith("net.minecraft.")) return super.findClass(name);
        URL resource = findResource(name.replace('.', '/') + ".class");
        if (resource == null) throw new ClassNotFoundException(name);
        try (var input = resource.openStream()) {
            ClassReader reader = new ClassReader(input.readAllBytes());
            ClassWriter writer = new ClassWriter(0);
            reader.accept(new PackageAccessFixer(Opcodes.ASM9, writer), 0);
            byte[] bytes = writer.toByteArray();
            return defineClass(name, bytes, 0, bytes.length);
        } catch (IOException error) { throw new ClassNotFoundException(name, error); }
    }

    public static void main(String[] args) throws Exception {
        var originalError = System.err;
        boolean failed = false;
        String[] entries = System.getProperty("java.class.path").split(java.io.File.pathSeparator);
        URL[] urls = new URL[entries.length];
        for (int i = 0; i < entries.length; i++) urls[i] = Path.of(entries[i]).toUri().toURL();
        try (var loader = new VanillaObjectTestHost(urls)) {
            Thread.currentThread().setContextClassLoader(loader);
            try {
                loader.loadClass(args[0]).getMethod("main", String[].class)
                        .invoke(null, (Object) Arrays.copyOfRange(args, 1, args.length));
            } catch (Throwable error) {
                error.printStackTrace(originalError);
                failed = true;
            }
        }
        if (failed) System.exit(1);
    }
}
