// Interacciones compartidas: filtros de listas y presentación en tarjetas para móvil.
(() => {
  const normalizar = (texto) => String(texto || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("es");

  const clonarConIdentificadoresNuevos = (celda, sufijo) => {
    const fragmento = document.createElement("div");
    fragmento.innerHTML = celda.innerHTML;
    const reemplazos = new Map();
    fragmento.querySelectorAll("[id]").forEach((nodo) => {
      const anterior = nodo.id;
      const nuevo = `${anterior}-${sufijo}`;
      reemplazos.set(anterior, nuevo);
      nodo.id = nuevo;
    });
    fragmento.querySelectorAll("label[for]").forEach((etiqueta) => {
      const nuevo = reemplazos.get(etiqueta.htmlFor);
      if (nuevo) etiqueta.htmlFor = nuevo;
    });
    return fragmento;
  };

  const crearTarjetasMoviles = () => {
    document.querySelectorAll("table[data-mobile-cards]").forEach((tabla) => {
      if (tabla.dataset.tarjetasCreadas === "1") return;
      const envoltorio = tabla.parentElement;
      const cabeceras = [...(tabla.tHead?.rows[0]?.cells || [])].map((celda) => celda.textContent.trim());
      const tarjetas = document.createElement("div");
      tarjetas.className = "tarjetas-registro-movil";
      const filas = [...(tabla.tBodies[0]?.rows || [])];

      filas.forEach((fila, indice) => {
        const celdas = [...fila.cells];
        if (!celdas.length) return;
        if (celdas.length === 1 && celdas[0].colSpan > 1) {
          const vacio = document.createElement("div");
          vacio.className = "tarjeta estado-vacio";
          vacio.innerHTML = celdas[0].innerHTML;
          tarjetas.append(vacio);
          return;
        }

        const tarjeta = document.createElement("article");
        tarjeta.className = "tarjeta fila-registro-movil";
        [...fila.attributes]
          .filter((atributo) => atributo.name.startsWith("data-filtro-"))
          .forEach((atributo) => tarjeta.setAttribute(atributo.name, atributo.value));

        const campos = document.createElement("dl");
        campos.className = "campos-registro-movil";
        celdas.forEach((celda, posicion) => {
          const titulo = cabeceras[posicion] || "";
          const contenido = clonarConIdentificadoresNuevos(celda, `movil-${indice}-${posicion}`);
          if (!titulo) {
            const acciones = document.createElement("div");
            acciones.className = "acciones-registro-movil";
            acciones.append(contenido);
            tarjeta.append(acciones);
            return;
          }
          const campo = document.createElement("div");
          campo.className = "campo-registro-movil";
          const etiqueta = document.createElement("dt");
          etiqueta.textContent = titulo;
          const valor = document.createElement("dd");
          valor.append(contenido);
          campo.append(etiqueta, valor);
          campos.append(campo);
        });
        tarjeta.prepend(campos);
        tarjetas.append(tarjeta);
      });

      tabla.dataset.tarjetasCreadas = "1";
      envoltorio.classList.add("tabla-con-tarjetas-movil");
      envoltorio.append(tarjetas);
    });
  };

  const inicializarFiltros = () => {
    document.querySelectorAll("[data-filtro-lista]").forEach((lista) => {
      const campo = lista.querySelector("[data-filtro-busqueda]");
      const botones = [...lista.querySelectorAll("[data-filtro-valor]")];
      const elementos = [...lista.querySelectorAll("[data-filtro-item]")];
      const estadoInicial = new Map(elementos.map((elemento) => [elemento, elemento.hidden]));
      const resultado = lista.querySelector("[data-filtro-resultados]");
      const vacio = lista.querySelector("[data-filtro-vacio]");
      let filtro = "*";

      const aplicar = () => {
        const termino = normalizar(campo?.value.trim());
        // Restablecer el ocultamiento propio del HTML permite medir la vista actual
        // aunque el filtro anterior haya escondido filas.
        elementos.forEach((elemento) => { elemento.hidden = estadoInicial.get(elemento); });
        const enDisenoActual = new Set(elementos.filter((elemento) => elemento.getClientRects().length > 0));
        let visibles = 0;
        elementos.forEach((elemento) => {
          const coincideTexto = !termino || normalizar(elemento.dataset.filtroTexto).includes(termino);
          const grupos = (elemento.dataset.filtroGrupo || "").split(/\s+/).filter(Boolean);
          const coincideGrupo = filtro === "*" || grupos.includes(filtro);
          elemento.hidden = estadoInicial.get(elemento) || !(coincideTexto && coincideGrupo);
          if (!elemento.hidden && enDisenoActual.has(elemento)) visibles += 1;
        });
        if (resultado) resultado.textContent = `${visibles} de ${enDisenoActual.size}`;
        if (vacio) vacio.hidden = enDisenoActual.size === 0 || visibles > 0;
      };

      campo?.addEventListener("input", aplicar);
      botones.forEach((boton) => boton.addEventListener("click", () => {
        filtro = boton.dataset.filtroValor || "*";
        botones.forEach((otro) => {
          const activo = otro === boton;
          otro.setAttribute("aria-pressed", String(activo));
          otro.classList.toggle("filtro-activo", activo);
        });
        aplicar();
      }));
      aplicar();
      window.addEventListener("resize", () => window.requestAnimationFrame(aplicar), { passive: true });
    });
  };

  const iniciar = () => {
    crearTarjetasMoviles();
    inicializarFiltros();
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", iniciar, { once: true });
  else iniciar();
})();
