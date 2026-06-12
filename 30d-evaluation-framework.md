# Marco de evaluación de 30 días — Pivot a Market Intelligence

> **CONGELADO — Día 0 (2026-06-12).** A partir de esta fecha, este documento
> es la referencia fija para los criterios A/B, pesos de decisión, condiciones
> de stop y límites de conclusión. Durante el ciclo de 30 días **no se
> modifican** los umbrales de `RegimeEngine`/`SymbolManager` ni los criterios
> aquí definidos para "hacer que los resultados parezcan mejores". Cualquier
> ajuste detectado como necesario durante el ciclo se documenta como hallazgo
> al cierre, no se aplica in-flight (ver §4, "Stop por contaminación del
> experimento").

**Propósito de este documento**: fijar, antes de ejecutar, qué se va a medir, cómo se decide (A) vs (B) por componente, qué peso tiene cada resultado en la decisión final, cuándo abortar el ciclo, y — lo más importante — qué NO se podrá concluir bajo ningún escenario, para evitar repetir el patrón que mató al motor de señales: declarar una validación que el dataset/ventana no podían sostener.

Este marco **no es un experimento de validación estadística**. Es un sistema de **utilidad perceptual falsable**: cada componente tiene un criterio explícito de fracaso (B) definido *antes* de ver los resultados, para que la conclusión no dependa de cómo "se sienta" el output.

---

## 0. Principio rector

> La pregunta que este ciclo puede responder es: **"¿el sistema, corriendo en vivo durante 30 días sobre el dataset disponible, produce al menos una señal de que merece la pena invertir en universo amplio?"**
>
> La pregunta que este ciclo NO puede responder es: **"¿Market Intelligence es la posición correcta de producto?"** Esa pregunta requiere universo amplio (decenas/cientos de símbolos) y un periodo de tiempo que cubra al menos un ciclo de mercado completo. No se finge lo contrario.

---

## 1. Componentes evaluados y su estatus de medibilidad

| Componente | Medible en esta ventana | Tipo de evaluación | Peso en decisión final |
|---|---|---|---|
| **Tiering / M3 en vivo** (promoción/democión, spike detection) | Sí — único componente cuya validez depende del *tiempo de ejecución real*, no del tamaño del dataset histórico | Falsable, criterio de adelanto temporal | **Dominante** |
| **Regime engine** (replay BTC/ETH, mayo) | Parcial — solo como sanity check de ruido/consistencia | Falsable (ruido vs. señal) | Secundario |
| **Breadth / health score** (n=2 símbolos) | **No** — matemáticamente trivial con 2 símbolos | Solo chequeo técnico de implementación (no de producto) | Ninguno (excluido de la decisión) |
| **Daily Report** | Parcial — depende de regime+breadth, hereda sus limitaciones | Falsable (comparación contra gráfico crudo) | Secundario, condicionado |

---

## 2. Criterios A/B por componente

### 2.1 Tiering / M3 en vivo — COMPONENTE DOMINANTE

Desplegar M3 en producción/shadow lo antes posible (días 1-3) para maximizar la ventana de acumulación de transiciones de tier reales (n esperado: 1-5 eventos en 30 días).

Para **cada** transición de tier observada (promoción o democión), evaluar dos dimensiones independientes:

**Dimensión 1 — Redetección vs. interpretación**
- (B) si la transición coincide 1:1 con un pico de volumen/precio que sería obvio mirando 5 segundos el gráfico crudo del símbolo, sin ningún contexto adicional.
- (A) si la transición incorpora información relativa al resto del universo (ej. "este símbolo se mueve distinto a sus pares" o "el volumen es anómalo *en relación* al resto del mercado, no solo en absoluto") que no sería evidente mirando el símbolo de forma aislada.

**Dimensión 2 — Ventaja temporal (NUEVO, añadido en esta revisión)**
- (B) si la promoción/democión se registra **después** (o simultáneamente) a que el movimiento ya sea de conocimiento/consenso general — es decir, el sistema "confirma lo que ya pasó", sin ventaja.
- (A) si la promoción/democión se registra **antes o durante** la formación del movimiento, de forma que un usuario que reciba la alerta tendría margen de acción que no tendría leyendo noticias/redes/charts en ese momento.

**Regla de falsación para tiering**:
- Si **todas** las transiciones observadas son (B) en la Dimensión 2 (sin ventaja temporal) → tiering es **(B)**, independientemente de la Dimensión 1.
- Si **al menos una** transición es (A) en ambas dimensiones → tiering es **(A)**, aunque sea n=1. Se documenta explícitamente como "n=1, no generalizable, pero es la única señal positiva posible en esta ventana".
- Si n=0 (ninguna transición ocurre en 30 días) → tiering es **inconcluso**, no (B). Se trata como un resultado de "ventana insuficiente", distinto de "mecanismo defectuoso" — y se reporta como tal, sin forzarlo a ninguna de las dos categorías.

### 2.2 Regime engine (replay BTC/ETH, mayo, modo retroactivo)

- (B) si el régimen (`risk_on`/`risk_off`/`rotational`) cambia de etiqueta más de ~1 vez/día sin un movimiento de precio/volumen correspondiente visible en el gráfico crudo — el régimen es más ruidoso que el precio mismo, no aporta resumen.
- (A) si las transiciones de régimen son poco frecuentes (días-semanas) y corresponden visualmente a cambios reales de carácter del mercado (tendencia sostenida vs. lateral, etc.).
- **Resultado intermedio explícito**: si el régimen es estable pero no se puede verificar si "acierta" (porque con 2 símbolos no hay forma independiente de saber qué régimen "debería" haber), se reporta como **"estable pero no verificable"** — no se redondea a (A).

### 2.3 Breadth / health score (n=2) — EXCLUIDO DE LA DECISIÓN

- No se evalúa como (A)/(B) de producto. Es matemáticamente trivial con 2 símbolos (ad_ratio y pct_above_trend solo pueden tomar un puñado de combinaciones: "ambos suben", "ambos bajan", "divergen").
- Único chequeo aplicable: **¿el código corre sin errores y produce valores internamente consistentes?** (chequeo de implementación, no de producto). Resultado: pasa/no pasa, sin entrar en la matriz de decisión.

### 2.4 Daily Market Intelligence Report

- (B) si, al mostrar el reporte junto al gráfico crudo de BTC/ETH del mismo día a una persona que no vio el código, la respuesta a "¿esto te dice algo que el gráfico no te decía?" es "no, es una descripción en palabras de lo que ya veo".
- (A) si la respuesta identifica al menos un elemento del reporte que aportó contexto no evidente en el gráfico (típicamente, esto solo puede venir de la sección de tiering, dado que breadth/regime heredan las limitaciones de 2.2/2.3).
- **Condicionado**: el resultado del Daily Report no se interpreta de forma independiente — si tiering es (B), el Daily Report no puede ser (A) de forma sustantiva (solo podría "parecer" útil por redacción, lo cual sería un falso positivo de presentación, no de contenido).

---

## 3. Pesos de decisión

La decisión final **no es un promedio** de los cuatro componentes. Es una regla jerárquica:

1. **Si tiering = (B)** → el sistema completo **no escala** como Market Intelligence con el approach actual, independientemente de los resultados de regime/report. Recomendación: **Pivot o Stop** (ver matriz §5). El resto de componentes ambiguos no compensan esto.
2. **Si tiering = (A)** (aunque sea n=1) → hay justificación para invertir en universo amplio + más tiempo de runtime. Los resultados de regime/report se usan para priorizar *qué* construir primero (ej. si regime es (B), no priorizar el regime engine en la siguiente fase; si report es (A) condicionado, priorizar el pipeline de reportes).
3. **Si tiering = inconcluso (n=0)** → no se puede tomar la decisión binaria. Se recomienda **extender la ventana de observación de M3** (no repetir todo el ciclo de 30 días, solo seguir corriendo M3 y revisar de nuevo en +15-30 días) antes de declarar Pivot/Stop. Esto es distinto de "fallar" — es "el experimento más informativo todavía no terminó".

---

## 4. Condiciones de stop (abortar el ciclo antes del día 30)

Detener el ciclo de evaluación antes de tiempo si ocurre cualquiera de:

- **Stop técnico**: M3 no puede desplegarse de forma estable en los primeros 5 días (errores de runtime, dependencias bloqueantes no resueltas). En ese caso, el ciclo completo se invalida porque el único componente con peso dominante no genera datos — no tiene sentido seguir "evaluando" regime/report sobre la base de que son secundarios a algo que no existe.
- **Stop por bug bloqueante no resuelto**: si los bugs P0 de `market_memory.symbol_id` y `jsonb` en BatchWriter (identificados en el hardening plan) no se resuelven en la semana 1, el Daily Report no puede generarse sobre datos correctos — se excluye el componente 2.4 del ciclo (no se aborta todo, pero se documenta como "no evaluado por bloqueo de infraestructura", distinto de (B)).
- **Stop por contaminación del experimento**: si durante el periodo se realizan cambios manuales a los umbrales de regime/tiering "para que produzcan resultados más razonables" — esto invalidaría cualquier conclusión, porque convertiría el experimento en exactamente el mismo proceso de "ajustar el umbral hasta que parezca funcionar" que produjo el umbral de confianza 60 fallido. Si se detecta esta necesidad durante el ciclo, se **documenta como hallazgo** (los umbrales por defecto no son razonables) pero no se ajusta in-flight — se reporta como parte del resultado, no se oculta ajustándolo.

---

## 5. Matriz de resultados y recomendación

| Tiering | Regime | Report | Recomendación |
|---|---|---|---|
| (A) | cualquiera | cualquiera | **Double Down en tiering/universo amplio.** Priorizar M3 a escala + acumulación de historial. Regime/report se reconsideran según su resultado individual, pero no bloquean. |
| (B) | cualquiera | cualquiera | **Pivot o Stop.** El mecanismo central de "emerging assets" no añade ventaja. Reconsiderar si el resto de la propuesta (regime/breadth como dashboard de contexto puro, sin pretensión de "detección temprana") tiene valor independiente — eso sería un *Reposition*, no un Double Down. |
| inconcluso (n=0) | (A) | — | **Extender ventana de M3** otros 15-30 días antes de decidir. El resultado de regime (A) no compensa la falta de datos de tiering, pero tampoco es señal negativa — solo no hay suficiente información todavía. |
| inconcluso (n=0) | (B) | — | **Señal de alerta, pero no decisiva.** Si además regime es ruidoso, hay un patrón preocupante (umbrales no calibrados en general), pero la decisión sigue pendiente de tiering. Extender ventana, y en paralelo revisar si los umbrales de regime necesitan recalibración antes de la siguiente ventana. |

---

## 6. Qué NO se puede concluir bajo ningún escenario

Independientemente del resultado, este ciclo de 30 días **no permite afirmar**:

1. Que "Market Intelligence" como categoría de producto es la posición correcta para Orvixa — eso requiere validación con universo amplio (decenas/cientos de símbolos) y un dataset que cubra múltiples regímenes de mercado reales, ninguno de los cuales existe hoy.
2. Que breadth/health score "funcionan" o "no funcionan" — son matemáticamente no evaluables con n=2, así que cualquier afirmación sobre su validez de producto no tiene base en este ciclo.
3. Que los umbrales actuales de regime (`_RISK_ON_AD_RATIO`, `_RISK_OFF_AD_RATIO`, etc.) o de spike-promotion son "correctos", aunque el resultado cualitativo sea positivo — un resultado (A) en regime con n=2 significa "no es ruidoso en este caso particular", no "el umbral 1.2/55.0/0.8/45.0 está calibrado".
4. Que un resultado (A) en tiering con n=1 generaliza a "el mecanismo de detección de activos emergentes funciona" — significa únicamente "existe al menos un caso que justifica seguir invirtiendo en investigarlo a mayor escala".
5. Que un resultado (B) en cualquier componente secundario (regime, report) invalida la tesis completa de Market Intelligence — solo invalida ese componente en esas condiciones; la tesis vive o muere por tiering, según la regla jerárquica de §3.
6. Nada sobre sector rotation / leadership detection — esos pilares ni siquiera tienen los datos base (taxonomía de sectores) para empezar a evaluarse, y quedan fuera de este marco por completo, no solo de este ciclo.

---

## 7. Resumen ejecutable

- **Días 1-3**: desplegar M3 en vivo (prioridad absoluta — es el reloj más escaso). Resolver bugs P0 (`market_memory.symbol_id`, `jsonb` BatchWriter).
- **Días 1-5**: retirar BUY/SELL/policy/confidence de cualquier superficie visible del producto.
- **Días 4-10**: replay de regime sobre BTC/ETH mayo (sanity check, no validación); chequeo técnico de breadth/health (pasa/no pasa, sin entrar en la decisión).
- **Días 10-25**: M3 corre en vivo, se documenta cada transición de tier con las dos dimensiones de §2.1 en el momento en que ocurre (no retroactivamente, para evitar sesgo de interpretación).
- **Días 20-28**: generar Daily Report a partir de los datos acumulados; evaluación ciega contra gráfico crudo (§2.4).
- **Día 30**: aplicar la matriz de §5. Si tiering = inconcluso, activar la extensión de ventana en lugar de forzar una decisión binaria.
