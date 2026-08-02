leer_fds usa os.readlink (no open) para reportar el destino del FD sin abrir sockets/pipes. Doble try/except cubre dos race conditions distintas: proceso murió (externo) y FD cerrado (interno).

leer_maps agrupa segmentos de memoria por categoría (heap, stack, text, lib, anon) leyendo /proc/<pid>/maps. Las direcciones vienen en hexadecimal (base 16). El nombre del archivo puede tener espacios, por eso uso split(maxsplit=5) — el patrón "no partas de más" que evita destrozar el último campo.

En Linux, un thread es un LWP (Light-Weight Process) y se expone en /proc/<pid>/task/<tid>/ con la misma estructura que un proceso. El TID de un thread coincide con el PID del proceso principal para el "main thread". Esto permite reutilizar el mismo parseo para procesos y threads.
Un socket es un canal de comunicación entre procesos (Unix sockets locales o TCP/UDP en red). En /proc/<pid>/fd/, un socket abierto se expone como symlink a socket:[N] donde N es su inode. leer_fds lo detecta por prefijo y solo reporta que existe — nunca lo abre, para no interferir con la comunicación real.

Un symlink es un tipo especial de archivo cuyo contenido es una ruta a otro archivo. El kernel sigue el symlink automáticamente cuando se lo abre, pero se puede consultar su destino sin seguirlo usando os.readlink(). En /proc/<pid>/fd/, cada FD abierto se expone como un symlink a su recurso real (archivo, socket, pipe, terminal); leer_fds usa readlink para reportar el destino sin efectos colaterales (no queremos leer del socket, solo reportar que existe).

Un file descriptor (FD) es un entero pequeño que el kernel asigna a un proceso como referencia a un recurso abierto (archivo, socket, pipe, terminal, dispositivo). Los FDs 0/1/2 siempre son stdin/stdout/stderr. Los FDs están en /proc/<pid>/fd/ como symlinks que apuntan al destino real: os.readlink() los resuelve sin abrir el destino. Es una aplicación del principio Unix "todo es un archivo": el mismo modelo (leer/escribir por número) sirve para cualquier recurso del sistema.
Heap: zona de memoria virtual donde un proceso guarda objetos creados dinámicamente durante la ejecución (listas, dicts, strings en Python; malloc() en C). Se expone en /proc/<pid>/maps con la etiqueta [heap]. Crece según necesidad. En Python, el garbage collector libera automáticamente los objetos que ya no se referencian.

Stack: zona de memoria donde se guardan las variables locales de las funciones activas. Se maneja automáticamente al entrar y salir de funciones. Tamaño limitado (~8 MB por defecto). Se expone como [stack] en /proc/<pid>/maps.
Stack overflow ≠ Out of memory. Stack overflow es "demasiadas funciones anidadas" (recursión sin control). Out of memory es "el heap creció más allá de la RAM disponible" (demasiados objetos). Un heap creciendo sin parar en /proc/<pid>/maps es indicador de memory leak.

En Linux, un thread es un LWP (Light-Weight Process) — una "tarea" del kernel que comparte memoria con otras tareas del mismo proceso. Cada thread tiene su propio TID, estado, y contadores; se expone en /proc/<pid>/task/<tid>/ con la misma estructura que un proceso. El TID del "main thread" coincide con el PID del proceso. Este modelo se llama 1:1 (una tarea del kernel por thread de usuario).

Un thread es un LWP (Light-Weight Process) que pertenece a un proceso y comparte con sus hermanos la memoria virtual, los file descriptors, el UID, el PID del proceso y el directorio de trabajo. Lo que no comparte: TID, stack propio, registros de CPU, estado, señales pendientes. Por eso son "livianos": crearlos no implica duplicar memoria. La contrapartida es que pueden pisarse mutuamente al acceder a variables compartidas → de ahí surge la necesidad de sincronización (locks, monitores, semáforos).

El monitor usa multiprocessing por dos razones: (1) el GIL de Python impide que threads ejecuten bytecode en paralelo, así que solo procesos escapan al límite de un core; (2) los procesos están aislados — un bug en un analizador no tumba al resto — mientras que threads comparten memoria y un fallo se propaga.

Copy-on-Write resuelve el problema de que fork() sería costoso si copiara toda la memoria del padre. En vez de eso, padre e hijo comparten las mismas páginas físicas marcadas read-only. Cuando alguno intenta escribir, el kernel copia esa página puntual y le da la copia privada al escritor. Consecuencia: fork() es rápido incluso para procesos de varios GB.

El Manager.dict() permite que procesos con espacios de memoria totalmente independientes compartan una estructura de datos. Por debajo, cada proceso se comunica por sockets con un proceso servidor que mantiene el dict real. Los cambios de un proceso son visibles para los demás en su próxima lectura.

Manejo del shutdown en múltiples capas: cada analizador captura BrokenPipeError/EOFError para salir limpio si el Manager muere primero. El proceso padre da timeout de 5s antes de escalar a SIGKILL con p.kill(). Esta redundancia elimina las trazas de error durante el shutdown, aunque el programa funcione correctamente aun sin ella.

Evidencia del funcionamiento de SIGUSR1: al mandar kill -SIGUSR1 <PID> al proceso padre, se genera un archivo dump_<timestamp>.json con el snapshot completo. El archivo tiene las 7 claves del snapshot con datos reales de todos los procesos del sistema. Es prueba física de que:

La señal fue capturada por el handler.
El handler escribió el byte en el pipe.
El loop principal leyó el byte y ejecutó dump_snapshot().
La serialización JSON del Manager.dict() funcionó correctamente.

Patrón self-pipe — por qué existe:

Problema: los handlers de señales corren en un contexto especial donde muchas funciones de Python NO son seguras (llamadas "no async-signal-safe"). Ejemplos: print, json.dump, open, logging. Si el handler las llama, el programa puede deadlockear, corromper estructuras internas, o crashear impredeciblemente.

Solución: el handler solo hace os.write(pipe, byte) — una de las pocas operaciones async-signal-safe según POSIX. El "trabajo pesado" (dump JSON, reload de config, prints) lo hace el loop principal en main.py, que lee bytes del pipe periódicamente y ejecuta la acción correspondiente en contexto normal.

Los códigos de un byte son mnemónicos del inglés: T (Terminate), R (Reload), D (Dump), V (Verbose), W (Winch). El pipe transporta un identificador; el loop mapea identificador → acción.

El pipe es no-bloqueante (fcntl.O_NONBLOCK) para que ni el handler pueda quedarse esperando al escribir ni el loop pueda quedarse esperando al leer. Todo fluye sin trabas.

"Cuando mando una señal como SIGUSR1 al proceso del monitor, el kernel interrumpe el proceso y ejecuta el handler que registré. El handler NO escribe el JSON directamente porque estaría en un contexto especial de señal donde no es seguro hacer operaciones complejas — solo escribe un byte identificador (b'D') en un pipe. Termina inmediatamente y el proceso retoma lo que estaba haciendo. El loop principal del main.py, que corre siempre en contexto normal, revisa el pipe cada medio segundo. Cuando encuentra el byte b'D', ejecuta dump_snapshot() tranquilamente y escribe el JSON. El handler y el loop están desacoplados: solo se comunican a través del pipe."


Qué es un handler: función registrada con signal.signal(SIGNAL, funcion) que el kernel ejecuta cuando llega esa señal al proceso. Se registra una vez y queda archivada; se dispara cuando corresponde. Interrumpe al proceso en el punto exacto donde estaba ejecutando, corre el handler, y devuelve el control al proceso donde había quedado.

Por qué el handler tiene que ser mínimo: durante la interrupción, las estructuras internas del proceso (buffers de I/O, gestores de memoria, mutex internos de Python) pueden estar en estados intermedios. Si el handler llama funciones que tocan esas mismas estructuras (como print, open, json.dump), las modifica cuando el proceso original todavía las está usando → corrupción, deadlocks, crashes. POSIX define una lista de operaciones seguras ("async-signal-safe"): os.write, _exit, signal. Casi ninguna función "normal" de Python está en la lista.

Cómo el self-pipe resuelve el problema: el handler solo hace os.write(pipe, byte) — operación async-signal-safe. El trabajo pesado (el dump, el reload) lo hace el loop principal fuera del contexto de señal. Handler y loop se comunican por el pipe; ninguno espera al otro.

Async-signal-safe = función garantizada por POSIX de ser segura de llamar dentro de un handler de señal. La lista es corta: os.write, os.read, _exit, signal, algunas más. La mayoría de funciones de Python NO son async-signal-safe: print, open, json.dump, logging. Si un handler llama funciones no-seguras y la señal llega mientras el proceso ya está ejecutando esas mismas funciones (por ejemplo un print mientras hay otro print pendiente), pueden corromper estructuras internas o generar deadlocks. Por eso el patrón self-pipe: el handler solo usa os.write (seguro) para dejar un byte en un pipe; el loop principal lee del pipe en contexto normal y ejecuta el trabajo pesado sin restricciones.

Race condition: Es un error donde el resultado correcto de un programa depende del orden relativo en que se intercalan operaciones sobre un recurso compartido, y ese orden no está garantizado por el lenguaje ni por el sistema operativo

Doble try/except en leer_fds: hay dos race conditions TOCTOU distintas anidadas, y cada una requiere su propia protección:

Externa (el proceso desaparece entre que se listó y se llamó leer_fds): si os.listdir('/proc/<pid>/fd') falla, devolvemos None para indicar "proceso no disponible".
Interna (un FD puntual se cierra durante la iteración, con el proceso todavía vivo): si os.readlink falla para un FD específico, saltamos ese FD con continue y seguimos leyendo los demás.

Sin la protección interna, un solo FD cerrado descartaría toda la lectura del proceso. Con ambas protecciones, la función es máximamente robusta a un /proc que se mueve mientras la leemos.

listar_pids(): devuelve la lista de PIDs vivos filtrando las carpetas numéricas de /proc. Único punto de entrada del sistema al catálogo de procesos.

leer_status(pid): lee /proc/<pid>/status, formato legible campo: valor. Devuelve dict con valores como strings. Trae máscaras de señales, memoria virtual con unidades, context switches. None si el proceso murió.

leer_stat(pid): lee /proc/<pid>/stat, formato compacto de 52 campos separados por espacios. Requiere rfind(')') para saltar la trampa del nombre entre paréntesis. Devuelve dict con valores ya convertidos a int. Trae jiffies de CPU (utime/stime), page faults, política de scheduling.

Cuándo usar cuál: status para datos legibles y máscaras; stat para métricas numéricas y CPU%.

PID recycling — limitación conocida del TP:

Linux recicla PIDs cuando un proceso muere y su número queda libre. Si entre listar_pids() y leer_stat(pid) un PID se recicla (muere el proceso original y nace otro con el mismo número), el TP leería datos válidos pero de un proceso distinto — silent data corruption, sin errores visibles.

El TP no implementa protección contra esto porque requeriría capturar start_time (/proc/<pid>/stat campo 22) en dos momentos y comparar. Como el monitor es de observación pasiva (no toma decisiones automatizadas), la probabilidad baja y el costo alto justifican no implementarlo.

Herramientas serias como htop sí lo implementan porque su superficie de exposición es mayor.


calcular_cpu_pct — análisis de correctitud:

No hay race conditions: el estado (historial_previo, historial_nuevo) es local al proceso analizador y no compartido. Sin embargo, es vulnerable a PID recycling: si entre dos vueltas del analizador un PID muere y se reasigna a otro proceso, el delta_jiffies puede ser negativo. El max(0.0, delta) enmascara el problema pero devuelve un CPU% erróneo (0% cuando el nuevo proceso sí usa CPU).

Mitigación disponible pero no implementada: capturar start_time (/proc/<pid>/stat campo 22) junto con los jiffies, y descartar el historial si start_time cambió entre vueltas. No implementado por costo/beneficio en un monitor pasivo.


CPU% se calcula como la velocidad de un auto:

El campo utime + stime de /proc/<pid>/stat es un odómetro: te dice cuántos jiffies (unidad de tiempo del kernel, 1s = 100 jiffies) consumió el proceso desde que nació. Una sola lectura no te dice a qué "velocidad" está usando CPU ahora.

Para saber la velocidad instantánea:

Leés el odómetro en T1: jiffies_prev.
Esperás un intervalo (delta_tiempo segundos).
Leés el odómetro en T2: jiffies_actual.
delta_jiffies = jiffies_actual - jiffies_prev.
Convertís a segundos de CPU consumidos: delta_jiffies / CLK_TCK.
Dividís por el tiempo real que pasó: (delta_jiffies / CLK_TCK) / delta_tiempo.
Multiplicás por 100 para tener porcentaje.

Por eso el analizador guarda un historial: necesita la lectura previa para restarla. La primera vuelta devuelve 0% porque no hay lectura previa para comparar.