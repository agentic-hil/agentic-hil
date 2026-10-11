/*
 * hq177, libopencm3 issue #528: both DAC channels of an STM32F446 driven from
 * one memory buffer by one DMA stream, on one timer trigger.
 */
#include <libopencm3/stm32/dac.h>
#include <libopencm3/stm32/dma.h>
#include <libopencm3/stm32/gpio.h>
#include <libopencm3/stm32/rcc.h>
#include <libopencm3/stm32/timer.h>

#include "dual_dac_dma.h"

/*
 * Arm both DAC channels, the DMA stream that feeds them and the timer that
 * triggers them. Nothing is output until the counter starts, on the last
 * line.
 *
 * `words` takes the `count` packed words and is what the stream reads, so it
 * has to outlive the run and be 4-aligned; `ch1` and `ch2` hold `count`
 * samples each; `prescaler` and `period` are TIM7's, counted from the APB1
 * timer clock.
 *
 * The body between the two marker lines is, line for line, the example the
 * documentation patch beside this file inserts into
 * lib/stm32/common/dac_common_all.c. It therefore carries no comment of any
 * kind: the block lands inside that file's Doxygen comment block, where a
 * comment delimiter would close the comment early and leave the file
 * uncompilable. What the five steps do:
 *
 *  1. Each 32-bit word carries both samples right-aligned in the layout of
 *     DAC_DHR12RD: channel 1 in bits 11:0, channel 2 in bits 27:16. Packing
 *     them explicitly is the part the library's old text never explained.
 *  2. The DAC is on APB1; its outputs are PA4 and PA5, left as analog inputs
 *     so the digital driver does not fight the DAC.
 *  3. TIM7's update event, as its TRGO, is the one trigger of both channels.
 *     The UG event loads the prescaler before the DAC listens to TRGO;
 *     without it the first period would run with the reset prescaler.
 *  4. On the STM32F446 the DAC channel 1 request is DMA1 stream 5 on
 *     channel 7. One 32-bit word per request goes into the dual register,
 *     circular, the memory address incrementing, the FIFO in direct mode.
 *  5. DMA is enabled for channel 1 only, so each trigger raises exactly one
 *     request. The first trigger outputs the pair
 *     dac_load_data_buffer_dual preloaded, every later one the word the DMA
 *     fetched after the trigger before it.
 */
void dual_dac_dma_start(uint32_t *words, const uint16_t *ch1,
			const uint16_t *ch2, uint16_t count,
			uint32_t prescaler, uint32_t period)
{
	/* hq177-example-begin */
	for (uint16_t i = 0; i < count; i++) {
		words[i] = (ch1[i] & DAC_DHR12RD_DACC1DHR_MSK) |
			   ((uint32_t)(ch2[i] & DAC_DHR12RD_DACC2DHR_MASK)
			    << DAC_DHR12RD_DACC2DHR_SHIFT);
	}

	rcc_periph_clock_enable(RCC_GPIOA);
	rcc_periph_clock_enable(RCC_DAC);
	rcc_periph_clock_enable(RCC_DMA1);
	rcc_periph_clock_enable(RCC_TIM7);
	gpio_mode_setup(GPIOA, GPIO_MODE_ANALOG, GPIO_PUPD_NONE, GPIO4 | GPIO5);

	timer_set_prescaler(TIM7, prescaler);
	timer_set_period(TIM7, period);
	timer_generate_event(TIM7, TIM_EGR_UG);
	timer_clear_flag(TIM7, TIM_SR_UIF);
	timer_set_master_mode(TIM7, TIM_CR2_MMS_UPDATE);

	dma_stream_reset(DMA1, DMA_STREAM5);
	dma_channel_select(DMA1, DMA_STREAM5, DMA_SxCR_CHSEL_7);
	dma_set_transfer_mode(DMA1, DMA_STREAM5,
			      DMA_SxCR_DIR_MEM_TO_PERIPHERAL);
	dma_set_peripheral_address(DMA1, DMA_STREAM5,
				   (uint32_t)&DAC_DHR12RD(DAC1));
	dma_set_peripheral_size(DMA1, DMA_STREAM5, DMA_SxCR_PSIZE_32BIT);
	dma_set_memory_address(DMA1, DMA_STREAM5, (uint32_t)words);
	dma_set_memory_size(DMA1, DMA_STREAM5, DMA_SxCR_MSIZE_32BIT);
	dma_enable_memory_increment_mode(DMA1, DMA_STREAM5);
	dma_set_number_of_data(DMA1, DMA_STREAM5, count);
	dma_enable_circular_mode(DMA1, DMA_STREAM5);
	dma_enable_direct_mode(DMA1, DMA_STREAM5);
	dma_set_priority(DMA1, DMA_STREAM5, DMA_SxCR_PL_HIGH);
	dma_enable_stream(DMA1, DMA_STREAM5);

	dac_trigger_enable(DAC1, DAC_CHANNEL_BOTH);
	dac_set_trigger_source(DAC1, DAC_CR_TSEL1_T7 | DAC_CR_TSEL2_T7);
	dac_dma_enable(DAC1, DAC_CHANNEL1);
	dac_load_data_buffer_dual(DAC1, ch1[count - 1], ch2[count - 1],
				  DAC_ALIGN_RIGHT12);
	dac_enable(DAC1, DAC_CHANNEL_BOTH);
	timer_enable_counter(TIM7);
	/* hq177-example-end */
}
