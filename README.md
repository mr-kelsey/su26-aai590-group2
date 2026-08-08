# Group 2 capstone project for AAI-590

This project is a part of the AAI-590 course in the Applied Artificial Intelligence Program at the University of San Diego (USD).

#### Installation
You should add instructions on how this project is to be used, installed, run, edited in others’ machine.
 
#### Project Intro/Objective

The purpose of this project is to train a spatiotemporal graph neural network to predict presence as a function of events in the city of San
Francisco. Our goal is to let an end user determine the foot traffic expected near their business on a specific day, through a front end where they enter an address and a date. The event schedule comes from a lookup table assembled from historical and announced events spanning 2016 through 2026, with estimated attendance drawn from the historical record. Weather enters the model through Open-Meteo's public API: forecasts for dates inside the forecast window, archived observations for past dates, and seasonal averages beyond both.

We are seeking to answer two questions: what is an event actually worth to the neighborhood around the event? and how does the presence ripple outward from a major event? To test this, we conduct a series of experiments in which we test whether the dataset we compile is suitable for a spatiotemporal graph neural network.

To do so, we establish a naive baseline, and a gradient boosted machine in order to determine how a model which is not informed by graph edges performs. We then compare a series of graph neural networks with varying edge types, using an ablation strategy to determine the effects of the edges, and comparing the resulting models to the gradient boosted machine.

#### Partner(s)/Contributor(s)

* [Stephen Farmer](https://github.com/Jungleislander)
* [Johnathan Kelsey](https://github.com/mr-kelsey)
* [Lucas Young](https://github.com/Giant-Leap-ai)

#### Methods Used
* Inferential Statistics
* Machine Learning
* Deep Learning
* Data Visualization
* Cloud Computing 
* Graph Neural Networks
* Temporal Dissaggregation

#### Technologies
* Python
* PyTorch
* AWS
* DuckDB
* JavaScript
* Claude Code

#### Project Description
We compiled a dataset using variety of private public data sources including bart ridership data, weather data event data, and presence data purchased through advan. In order to determine relative economic impact to regions within San Francisco, we divided the city in two different ways. The first is a series of concentric circles surrounding Oracle Park. The second method is creating 452 250 M^2 blocks within the city and determining a suitable method for grouping points of interest into these squares,/

#### License
GNU GENERAL PUBLIC LICENSE Version 3

#### Acknowledgments
You can mention and thank your professors and those who technically helped you during the project. 
