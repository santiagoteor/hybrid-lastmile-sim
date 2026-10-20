# M6 — proyecto completo consolidado (GLIMS + ILS)

Este ZIP contiene el código compacto, configuraciones, datos de las tres ciudades y pruebas, sin resultados históricos, entornos virtuales ni módulos legacy.

## Ejecutar

1. Haz copia de seguridad de tu proyecto actual.
2. Extrae el ZIP en una carpeta **nueva**, no encima de la instalación anterior.
3. Desde la carpeta nueva, activa tu entorno Python y, si es necesario, instala el proyecto con `pip install -e .` (si usas tu instalación editable anterior, ejecuta ese comando desde la nueva carpeta).
4. Revisa `config/infrastructure.json` (ruta de caché GLIMS).
5. Ejecuta:

```powershell
python -m hybrid_lastmile.experiment_cli `
  --experiment experiments/barcelona_m6_smoke.json `
  --infrastructure config/infrastructure.json `
  --data-root data `
  --output results/m6
```

El experimento de ejemplo usa ILS de GLIMS para abastecimiento PUDO y ejecuta semillas 42 y 101. Los parámetros de ILS están en `supply_routing.routing` y son los de `RoutingAlgorithmConfig` de GLIMS. Cambia `algorithm` a `cws` para comparar. El nombre del directorio de cada ejecución incluye el algoritmo y un hash de la configuración de routing, evitando sobrescrituras CWS/ILS.

Los nombres antiguos `glims_supply` y `glims_supply_routing` se normalizan automáticamente para mantener compatibilidad; no hace falta `migrar_configuracion.py`. GLIMS es obligatorio.

**Alcance:** elección PUDO/HD, asignación bicicleta/van, manifiestos, selección de CC y rutas CWS/ILS de abastecimiento PUDO. Las rutas HD, los costes/emisiones integrados y la escalabilidad de 60k siguen pendientes. No se han ejecutado aquí pruebas con OSRM local.
